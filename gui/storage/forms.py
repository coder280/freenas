# Copyright 2010 iXsystems, Inc.
# All rights reserved
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted providing that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
#####################################################################
from collections import defaultdict, OrderedDict
from datetime import datetime, time
from decimal import Decimal
import logging
import os
import re
import ssl
import tempfile
import uuid

from formtools.wizard.views import SessionWizardView
from django.core.files.storage import FileSystemStorage
from django.core.urlresolvers import reverse
from django.forms import FileField
from django.forms.formsets import BaseFormSet, formset_factory
from django.http import HttpResponse, QueryDict
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext_lazy as _, ungettext

from dojango import forms
from dojango.forms import CheckboxSelectMultiple
from freenasUI import choices
from freenasUI.account.models import bsdUsers
from freenasUI.common import humanize_number_si, humansize_to_bytes
from freenasUI.common.forms import ModelForm, Form, mchoicefield
from freenasUI.freeadmin.apppool import appPool
from freenasUI.freeadmin.forms import (
    CronMultiple, UserField, GroupField, WarningSelect,
    PathField,
)
from freenasUI.freeadmin.utils import key_order
from freenasUI.freeadmin.views import JsonResp
from freenasUI.middleware import zfs
from freenasUI.middleware.client import client
from freenasUI.middleware.exceptions import MiddlewareError
from freenasUI.middleware.form import MiddlewareModelForm
from freenasUI.middleware.notifier import notifier
from freenasUI.middleware.util import JobAborted, JobFailed, upload_job_and_wait
from freenasUI.services.exceptions import ServiceFailed
from freenasUI.services.models import iSCSITargetExtent, services
from freenasUI.storage import models
from freenasUI.storage.widgets import UnixPermissionField
from freenasUI.support.utils import dedup_enabled
from middlewared.client import Client
from pyVim import connect, task as VimTask
from pyVmomi import vim


attrs_dict = {'class': 'required', 'maxHeight': 200}

log = logging.getLogger('storage.forms')

DEDUP_WARNING = _(
    "Enabling dedup can drastically reduce performance and<br />"
    "affect the ability to access data. Compression usually<br />"
    "offers similar space savings with much lower<br />"
    "performance impact and overhead.<br />")

RE_HOUR = re.compile(r'(?P<hour>\d{2}):(?P<min>\d{2}):(?P<sec>\d{2})')


def fix_time_fields(data, names):
    for name in names:
        if name not in data:
            continue
        search = RE_HOUR.search(data[name])
        data[name] = time(
            hour=int(search.group("hour")),
            minute=int(search.group("min")),
            second=int(search.group("sec")),
        )


class Disk(object):

    dev = None
    dtype = None
    number = None
    size = None

    def __init__(self, devname, size, serial=None):
        reg = re.search(r'^(.*?)([0-9]+)', devname)
        if reg:
            self.dtype, number = reg.groups()
            self.number = int(number)
        else:
            self.dtype = devname
            self.number = None
        self.size = size
        self.serial = serial
        self.human_size = humanize_number_si(size)
        self.dev = devname

    def __lt__(self, other):
        if self.human_size == other.human_size:
            if self.dtype == other.dtype:
                return self.number < other.number
            return self.dtype < other.dtype
        return self.size > other.size

    def __repr__(self):
        return '<Disk: %s>' % str(self)

    def __str__(self):
        extra = ' %s' % (self.serial,) if self.serial else ''
        return '%s (%s)%s' % (self.dev, humanize_number_si(self.size), extra)

    def __iter__(self):
        yield self.dev
        yield str(self)


def _clean_zfssize_fields(form, attrs, prefix):

    cdata = form.cleaned_data
    for field in [prefix + x for x in attrs]:
        if field not in cdata:
            cdata[field] = ''

    r = re.compile(r'^(\d+(?:\.\d+)?)([BKMGTP](?:iB)?)?$', re.I)
    msg = _("Specify the size with IEC suffixes or 0, e.g. 10 GiB")

    for attr in attrs:
        formfield = '%s%s' % (prefix, attr)
        match = r.match(cdata[formfield].replace(' ', ''))

        if not match and cdata[formfield] != "0":
            form._errors[formfield] = form.error_class([msg])
            del cdata[formfield]
        elif match:
            number, suffix = match.groups()
            if suffix and suffix.lower().endswith('ib'):
                cdata[formfield] = '%s%s' % (number, suffix[0])
            try:
                Decimal(number)
            except Exception:
                form._errors[formfield] = form.error_class([
                    _("%s is not a valid number") % (number, ),
                ])
                del cdata[formfield]
    return cdata


def _inherit_choices(choices, inheritvalue):
    nchoices = []
    for value, name in choices:
        if value == 'inherit':
            name += ' (%s)' % inheritvalue
        nchoices.append((value, name))
    return nchoices


class VolumeMixin(object):

    def clean_volume_name(self):
        vname = self.cleaned_data['volume_name']
        if vname and not re.search(r'^[a-z][-_.a-z0-9]*$', vname, re.I):
            raise forms.ValidationError(_(
                "The volume name must start with "
                "letters and may include numbers, \"-\", \"_\" and \".\" ."))
        if models.Volume.objects.filter(vol_name=vname).exists():
            raise forms.ValidationError(
                _("A volume with that name already exists."))
        if vname in ('log',):
            raise forms.ValidationError(_('\"log\" is a reserved word and thus cannot be used'))
        elif re.search(r'^c[0-9].*', vname) or \
                re.search(r'^mirror.*', vname) or \
                re.search(r'^spare.*', vname) or \
                re.search(r'^raidz.*', vname):
            raise forms.ValidationError(_(
                "The volume name may NOT start with c[0-9], mirror, "
                "raidz or spare"
            ))
        return vname


class VolumeManagerForm(VolumeMixin, Form):
    volume_name = forms.CharField(
        max_length=30,
        required=False)
    volume_add = forms.CharField(
        max_length=30,
        required=False)
    encryption = forms.BooleanField(
        required=False,
        initial=False,
    )
    encryption_inirand = forms.BooleanField(
        initial=False,
        required=False,
    )
    dedup = forms.ChoiceField(
        choices=choices.ZFS_DEDUP,
        initial="off",
        required=False,
    )

    def __init__(self, *args, **kwargs):
        """
        Compatibility layer required for our API framework
        """
        if 'instance' in kwargs:
            kwargs.pop('instance')
        super(VolumeManagerForm, self).__init__(*args, **kwargs)

    def is_valid(self):
        valid = super(VolumeManagerForm, self).is_valid()
        vdevFormSet = formset_factory(
            VolumeVdevForm,
            formset=VdevFormSet,
        )
        self._formset = vdevFormSet(self.data, prefix='layout')
        self._formset.pform = self
        fsvalid = self._formset.is_valid()
        if not fsvalid:
            nonformerrors = self._formset.non_form_errors()
            if nonformerrors:
                self._errors['__all__'] = self.error_class(nonformerrors)
        return valid and fsvalid

    def clean(self):
        vname = (
            self.cleaned_data.get("volume_name") or
            self.cleaned_data.get("volume_add")
        )
        if not vname:
            self._errors['__all__'] = self.error_class([
                _("You must specify a new volume name or select an existing "
                  "ZFS volume to append a virtual device"),
            ])
        else:
            self.cleaned_data["volume_name"] = vname
        return self.cleaned_data

    def save(self):
        formset = self._formset
        volume_name = self.cleaned_data.get("volume_name")
        init_rand = self.cleaned_data.get("encryption_inirand", False)
        if self.cleaned_data.get("encryption", False):
            volume_encrypt = 1
        else:
            volume_encrypt = 0
        dedup = self.cleaned_data.get("dedup", False)

        volume = scrub = None
        try:
            vols = models.Volume.objects.filter(vol_name=volume_name)
            if vols.count() > 0:
                volume = vols[0]
                add = True
            else:
                add = False
                volume = models.Volume(vol_name=volume_name, vol_encrypt=volume_encrypt)
                volume.save()

            self.volume = volume

            grouped = OrderedDict()
            # FIXME: Make log as log mirror
            for i, form in enumerate(formset):
                if not form.cleaned_data.get('vdevtype'):
                    continue
                grouped[i] = {
                    'type': form.cleaned_data.get("vdevtype"),
                    'disks': form.cleaned_data.get("disks"),
                }

            if add:
                for gtype, group in list(grouped.items()):
                    notifier().zfs_volume_attach_group(
                        volume,
                        group)

            else:
                notifier().create_volume(volume, groups=grouped, init_rand=init_rand)

                if dedup:
                    notifier().zfs_set_option(volume.vol_name, "dedup", dedup)

                scrub = models.Scrub.objects.create(scrub_volume=volume)
        except Exception as e:
            if not add and volume:
                volume.delete(destroy=False, cascade=False)
            if scrub:
                scrub.delete()
            raise e

        if volume.vol_encrypt >= 2 and add:
            # FIXME: ask current passphrase to the user
            notifier().geli_passphrase(volume, None)
            volume.vol_encrypt = 1
            volume.save()

        # Send geli keyfile to the other node
        _n = notifier()
        if volume_encrypt > 0 and not _n.is_freenas() and _n.failover_licensed():
            with client as c:
                _n.sync_file_send(c, volume.get_geli_keyfile())

        # This must be outside transaction block to make sure the changes
        # are committed before the call of ix-fstab
        notifier().reload("disk")
        if not add:
            notifier().start("ix-syslogd")
            notifier().restart("system_datasets")
        # For scrub cronjob
        notifier().restart("cron")

        # restart smartd to enable monitoring for any new drives added
        if (services.objects.get(srv_service='smartd').srv_enable):
            notifier().restart("smartd")

        # ModelForm compatibility layer for API framework
        self.instance = volume

        return volume


class VolumeVdevForm(Form):
    vdevtype = forms.CharField(
        max_length=20,
    )
    disks = forms.CharField(
        max_length=800,
        widget=forms.widgets.SelectMultiple(),
    )

    def clean_disks(self):
        vdev = self.cleaned_data.get("vdevtype")
        # TODO: Safe?
        disks = eval(self.cleaned_data.get("disks"))
        errmsg = _("You need at least %d disks")
        if vdev == "mirror" and len(disks) < 2:
            raise forms.ValidationError(errmsg % 2)
        elif vdev == "raidz" and len(disks) < 3:
            raise forms.ValidationError(errmsg % 3)
        elif vdev == "raidz2" and len(disks) < 4:
            raise forms.ValidationError(errmsg % 4)
        elif vdev == "raidz3" and len(disks) < 5:
            raise forms.ValidationError(errmsg % 5)
        return disks

    def clean(self):
        if (
            self.cleaned_data.get("vdevtype") == "log" and
            len(self.cleaned_data.get("disks")) > 1
        ):
            self.cleaned_data["vdevtype"] = "log mirror"
        return self.cleaned_data


class VdevFormSet(BaseFormSet):

    def _clean_vdevtype(self, vdevfound, vdevtype):
        if vdevtype in (
            'cache',
            'log',
            'log mirror',
            'spare',
        ):
            if vdevtype == 'log mirror':
                name = 'log'
            else:
                name = vdevtype
            if vdevfound[name] is True:
                raise forms.ValidationError(_(
                    'Only one row for the vitual device of type %s'
                    ' is allowed.'
                ) % name)
            else:
                vdevfound[name] = True

    def clean(self):
        if any(self.errors):
            # Don't bother validating the formset unless each form
            # is valid on its own
            return

        vdevfound = defaultdict(lambda: False)
        if not self.pform.cleaned_data.get("volume_add"):
            """
            We need to make sure at least one vdev is a
            data vdev (non-log/cache/spare)
            """
            has_datavdev = False
            datatype = None
            for i in range(0, self.total_form_count()):
                form = self.forms[i]
                vdevtype = form.cleaned_data.get('vdevtype')
                if vdevtype in (
                    'mirror', 'stripe', 'raidz', 'raidz2', 'raidz3'
                ):
                    has_datavdev = True
                    if datatype is not None and datatype != vdevtype:
                        raise forms.ValidationError(_(
                            "You are not allowed to create a volume with "
                            "different data vdev types (%(vdev1)s and "
                            "%(vdev2)s)"
                        ) % {
                            'vdev1': datatype,
                            'vdev2': vdevtype,
                        })
                    datatype = vdevtype
                    continue
                self._clean_vdevtype(vdevfound, vdevtype)
            if not has_datavdev:
                raise forms.ValidationError(_("You need a data disk group"))
        else:
            zpool = notifier().zpool_parse(
                self.pform.cleaned_data.get("volume_add")
            )

            for i in range(0, self.total_form_count()):
                form = self.forms[i]
                vdevtype = form.cleaned_data.get('vdevtype')
                if not vdevtype:
                    continue

                if vdevtype in (
                    'cache',
                    'log',
                    'log mirror',
                    'spare',
                ):
                    self._clean_vdevtype(vdevfound, vdevtype)

            for vdev in zpool.data:

                for i in range(0, self.total_form_count()):
                    errors = []
                    form = self.forms[i]
                    vdevtype = form.cleaned_data.get('vdevtype')
                    if not vdevtype:
                        continue

                    if vdevtype in (
                        'cache',
                        'log',
                        'log mirror',
                        'spare',
                    ):
                        continue

                    disks = form.cleaned_data.get('disks')

                    if vdev.type != vdevtype:
                        errors.append(_(
                            "You are trying to add a virtual device of type "
                            "'%(addtype)s' in a pool that has a virtual "
                            "device of type '%(vdevtype)s'"
                        ) % {
                            'addtype': vdevtype,
                            'vdevtype': vdev.type,
                        })

                    if len(disks) != len(list(iter(vdev))):
                        errors.append(_(
                            "You are trying to add a virtual device consisting"
                            " of %(addnum)s device(s) in a pool that has a "
                            "virtual device consisting of %(vdevnum)s device(s)"
                        ) % {
                            'addnum': len(disks),
                            'vdevnum': len(list(iter(vdev))),
                        })
                    if errors:
                        raise forms.ValidationError(errors[0])


class ZFSVolumeWizardForm(Form):
    volume_name = forms.CharField(
        max_length=30,
        label=_('Volume name'),
        required=False)
    volume_disks = forms.MultipleChoiceField(
        choices=(),
        widget=forms.SelectMultiple(attrs=attrs_dict),
        label=_('Member disks'),
        required=False)
    group_type = forms.ChoiceField(
        choices=(),
        widget=forms.RadioSelect(attrs=attrs_dict),
        required=False)
    enc = forms.BooleanField(
        label=_('Encryption'),
        required=False,
    )
    encini = forms.BooleanField(
        label=_('Initialize Safely'),
        required=False,
    )
    dedup = forms.ChoiceField(
        label=_('ZFS Deduplication'),
        choices=choices.ZFS_DEDUP,
        initial="off",
    )

    def __init__(self, *args, **kwargs):
        super(ZFSVolumeWizardForm, self).__init__(*args, **kwargs)
        self.fields['volume_disks'].choices = self._populate_disk_choices()
        qs = models.Volume.objects.filter()
        if qs.exists():
            self.fields['volume_add'] = forms.ChoiceField(
                label=_('Volume add'),
                required=False)
            self.fields['volume_add'].choices = [
                ('', '-----')
            ] + [(x.vol_name, x.vol_name) for x in qs]
            self.fields['volume_add'].widget.attrs['onChange'] = (
                'zfswizardcheckings(true);')

        self.fields['enc'].widget.attrs['onChange'] = (
            'zfswizardcheckings(true);')

        grouptype_choices = (
            ('mirror', 'mirror'),
            ('stripe', 'stripe'),
        )
        if "volume_disks" in self.data:
            disks = self.data.getlist("volume_disks")
        else:
            disks = []
        if len(disks) >= 3:
            grouptype_choices += (('raidz', 'RAID-Z'), )
        if len(disks) >= 4:
            grouptype_choices += (('raidz2', 'RAID-Z2'), )
        if len(disks) >= 5:
            grouptype_choices += (('raidz3', 'RAID-Z3'), )
        self.fields['group_type'].choices = grouptype_choices

        # dedup = _dedup_enabled()
        dedup = True
        if not dedup:
            self.fields['dedup'].widget.attrs['readonly'] = True
            self.fields['dedup'].widget.attrs['class'] = (
                'dijitSelectDisabled dijitDisabled')

    def _populate_disk_choices(self):

        disks = []
        _n = notifier()

        if hasattr(_n, 'failover_status'):
            from freenasUI.truenas import ses
            encs = ses.Enclosures()
        else:
            encs = None

        # Grab disk list
        # Root device already ruled out
        for disk, info in list(_n.get_disks().items()):
            serial = info.get('ident', '')
            if encs:
                try:
                    ele = encs.find_device_slot(info['devname'])
                    serial = '%s/ %s' % (
                        '%s ' if serial else '',
                        ele.enclosure.devname,
                    )
                except Exception:
                    pass
            disks.append(Disk(
                info['devname'],
                info['capacity'],
                serial=serial,
            ))

        # Exclude what's already added
        used_disks = []
        for v in models.Volume.objects.all():
            used_disks.extend(v.get_disks())

        qs = iSCSITargetExtent.objects.filter(iscsi_target_extent_type='Disk')
        used_disks.extend([i.get_device()[5:] for i in qs])
        for d in list(disks):
            if d.dev in used_disks:
                disks.remove(d)

        choices = sorted(disks)
        choices = [tuple(d) for d in choices]
        return choices

    def clean_volume_name(self):
        vname = self.cleaned_data['volume_name']
        if vname and not re.search(r'^[a-z][-_.a-z0-9]*$', vname, re.I):
            raise forms.ValidationError(_(
                "The volume name must start with "
                "letters and may include numbers, \"-\", \"_\" and \".\" ."))
        if models.Volume.objects.filter(vol_name=vname).exists():
            raise forms.ValidationError(_(
                "A volume with that name already exists."
            ))
        return vname

    def clean_group_type(self):
        if 'volume_disks' not in self.cleaned_data or \
                len(self.cleaned_data['volume_disks']) > 1 and \
                self.cleaned_data['group_type'] in (None, ''):
            raise forms.ValidationError(_("This field is required."))
        return self.cleaned_data['group_type']

    def clean(self):
        cleaned_data = self.cleaned_data
        volume_name = cleaned_data.get("volume_name", "")
        disks = cleaned_data.get("volume_disks")
        if volume_name and cleaned_data.get("volume_add"):
            self._errors['__all__'] = self.error_class([
                _("You cannot select an existing ZFS volume and specify a new "
                  "volume name"),
            ])
        elif not(volume_name or cleaned_data.get("volume_add")):
            self._errors['__all__'] = self.error_class([
                _("You must specify a new volume name or select an existing "
                    "ZFS volume to append a virtual device"),
            ])
        elif not volume_name:
            volume_name = cleaned_data.get("volume_add")

        if len(disks) == 0 and models.Volume.objects.filter(
                vol_name=volume_name).count() == 0:
            msg = _("This field is required")
            self._errors["volume_disks"] = self.error_class([msg])
            del cleaned_data["volume_disks"]

        if volume_name in ('log',):
            msg = _("\"log\" is a reserved word and thus cannot be used")
            self._errors["volume_name"] = self.error_class([msg])
            cleaned_data.pop("volume_name", None)
        elif re.search(r'^c[0-9].*', volume_name) or \
                re.search(r'^mirror.*', volume_name) or \
                re.search(r'^spare.*', volume_name) or \
                re.search(r'^raidz.*', volume_name):
            msg = _(
                "The volume name may NOT start with c[0-9], mirror, "
                "raidz or spare"
            )
            self._errors["volume_name"] = self.error_class([msg])
            cleaned_data.pop("volume_name", None)

        return cleaned_data

    def done(self, request, events):
        # Construct and fill forms into database.
        volume_name = (
            self.cleaned_data.get("volume_name") or
            self.cleaned_data.get("volume_add")
        )
        disk_list = self.cleaned_data['volume_disks']
        dedup = self.cleaned_data.get("dedup", False)
        init_rand = self.cleaned_data.get("encini", False)
        if self.cleaned_data.get("enc", False):
            volume_encrypt = 1
        else:
            volume_encrypt = 0

        if (len(disk_list) < 2):
            group_type = 'stripe'
        else:
            group_type = self.cleaned_data['group_type']

        volume = scrub = None
        try:
            vols = models.Volume.objects.filter(vol_name=volume_name)
            if vols.count() == 1:
                volume = vols[0]
                add = True
            else:
                add = False
                volume = models.Volume(vol_name=volume_name, vol_encrypt=volume_encrypt)
                volume.save()

            self.volume = volume

            zpoolfields = re.compile(r'zpool_(.+)')
            grouped = OrderedDict()
            grouped['root'] = {'type': group_type, 'disks': disk_list}
            for i, gtype in list(request.POST.items()):
                if zpoolfields.match(i):
                    if gtype == 'none':
                        continue
                    disk = zpoolfields.search(i).group(1)
                    if gtype in grouped:
                        # if this is a log vdev we need to mirror it for safety
                        if gtype == 'log':
                            grouped[gtype]['type'] = 'log mirror'
                        grouped[gtype]['disks'].append(disk)
                    else:
                        grouped[gtype] = {'type': gtype, 'disks': [disk, ]}

            if len(disk_list) > 0 and add:
                notifier().zfs_volume_attach_group(volume, grouped['root'])

            if add:
                for grp_type in grouped:
                    if grp_type in ('log', 'cache', 'spare'):
                        notifier().zfs_volume_attach_group(
                            volume,
                            grouped.get(grp_type)
                        )

            else:
                notifier().create_volume(volume, groups=grouped, init_rand=init_rand)

                if dedup:
                    notifier().zfs_set_option(volume.vol_name, "dedup", dedup)

                scrub = models.Scrub.objects.create(scrub_volume=volume)

                try:
                    notifier().zpool_enclosure_sync(volume.vol_name)
                except Exception as e:
                    log.error("Error syncing enclosure: %s", e)
        except Exception:
            if volume:
                volume.delete(destroy=False, cascade=False)
            if scrub:
                scrub.delete()
            raise

        # This must be outside transaction block to make sure the changes
        # are committed before the call of ix-fstab
        notifier().reload("disk")
        # For scrub cronjob
        notifier().restart("cron")
        super(ZFSVolumeWizardForm, self).done(request, events)


class VolumeImportForm(Form):

    volume_disks = forms.ChoiceField(
        choices=(),
        widget=forms.Select(attrs=attrs_dict),
        label=_('Member disk'),
        help_text=_("This is the disk with the non-zfs filesystem. "
                    "It will be mounted, its data copied over to the path "
                    "specified in the 'Destination' field below and "
                    "then unmounted. Importing non-zfs disks permanently "
                    "as a Volume is deprecated"),
    )
    volume_fstype = forms.ChoiceField(
        choices=((x, x) for x in ('UFS', 'NTFS', 'MSDOSFS', 'EXT2FS')),
        widget=forms.RadioSelect(attrs=attrs_dict),
        label=_('File System type'),
    )
    volume_msdosfs_locale = forms.ChoiceField(
        label=_("MSDOSFS locale"),
        choices=(),
        widget=forms.Select(attrs=attrs_dict),
        required=False,
    )

    volume_dest_path = PathField(
        label=_("Destination"),
        help_text=_("This must be a dataset/folder in an existing Volume"),
    )

    def __init__(self, *args, **kwargs):
        super(VolumeImportForm, self).__init__(*args, **kwargs)
        self.fields['volume_disks'].choices = self._populate_disk_choices()
        with client as c:
            self.fields['volume_msdosfs_locale'].choices = [('', 'Default')] + [
                (locale, locale)
                for locale in c.call('pool.import_disk_msdosfs_locales')
            ]

    def _populate_disk_choices(self):

        used_disks = []
        for v in models.Volume.objects.all():
            used_disks.extend(v.get_disks())

        qs = iSCSITargetExtent.objects.filter(iscsi_target_extent_type='Disk')
        diskids = [i[0] for i in qs.values_list('iscsi_target_extent_path')]
        used_disks.extend([d.disk_name for d in models.Disk.objects.filter(
            disk_identifier__in=diskids)])

        n = notifier()
        # Grab partition list
        # NOTE: This approach may fail if device nodes are not accessible.
        _parts = n.get_partitions()
        for name, part in list(_parts.items()):
            for i in used_disks:
                if re.search(r'^%s([ps]|$)' % i, part['devname']) is not None:
                    _parts.pop(name, None)

        parts = []
        for name, part in list(_parts.items()):
            parts.append(Disk(part['devname'], part['capacity']))

        choices = sorted(parts)
        choices = [tuple(p) for p in choices]
        return choices

    def clean(self):
        cleaned_data = self.cleaned_data
        devpath = "/dev/%s" % (cleaned_data.get('volume_disks', []), )
        isvalid = notifier().precheck_partition(
            devpath,
            cleaned_data.get('volume_fstype', ''))
        if not isvalid:
            msg = _(
                "The selected disks were not verified for these import "
                "rules. Filesystem check failed."
            )
            self._errors["volume_fstype"] = self.error_class([msg])
        path = cleaned_data.get("volume_dest_path")
        if path is None or not os.path.exists(path):
            self._errors["volume_dest_path"] = self.error_class(
                [_("The path %s does not exist.\
                    This must be a dataset/folder in an existing Volume" % path)])
        return cleaned_data


def show_decrypt_condition(wizard):
    cleaned_data = wizard.get_cleaned_data_for_step('0') or {}
    if cleaned_data.get("step") == "decrypt":
        return True
    else:
        return False


class AutoImportWizard(SessionWizardView):
    file_storage = FileSystemStorage(location='/var/tmp/firmware')

    def get_template_names(self):
        return [
            'storage/autoimport_wizard_%s.html' % self.get_step_index(),
            'storage/autoimport_wizard.html',
        ]

    def process_step(self, form):
        proc = super(AutoImportWizard, self).process_step(form)
        """
        We execute the form done method if there is one, for each step
        """
        if hasattr(form, 'done'):
            retval = form.done(
                request=self.request,
                form_list=self.form_list,
                wizard=self)
            if self.get_step_index() == self.steps.count - 1:
                self.retval = retval
        return proc

    def render_to_response(self, context, **kwargs):
        response = super(AutoImportWizard, self).render_to_response(
            context,
            **kwargs
        )
        # This is required for the workaround dojo.io.frame for file upload
        if not self.request.is_ajax():
            return HttpResponse(
                "<html><body><textarea>" +
                response.rendered_content +
                "</textarea></boby></html>")
        return response

    def done(self, form_list, **kwargs):

        appPool.hook_form_init('AutoImportWizard', self, form_list, **kwargs)

        cdata = self.get_cleaned_data_for_step('1') or {}
        enc_disks = cdata.get("disks", [])
        key = cdata.get("key")
        if key:
            key.seek(0)
        passphrase = cdata.get("passphrase")

        cdata = self.get_cleaned_data_for_step('2') or {}
        vol = cdata['volume']

        arg = {
            'guid': vol['guid'],
            'devices': enc_disks,
            'passphrase': passphrase,
        }

        if enc_disks:
            try:
                upload_job_and_wait(key, 'pool.import_pool', arg)
            except JobAborted:
                raise MiddlewareError(_('Import job aborted'))
            except JobFailed as e:
                raise MiddlewareError(_('Import job failed: %s') % e.value)
        else:
            with client as c:
                c.call('pool.import_pool', arg, job=True)

        events = ['loadalert()']
        appPool.hook_form_done('AutoImportWizard', self, self.request, events)

        return JsonResp(
            self.request,
            message=str(_("Volume imported")),
            events=events,
        )


class AutoImportChoiceForm(Form):
    step = forms.ChoiceField(
        choices=(
            ('import', _("No: Skip to import")),
            ('decrypt', _("Yes: Decrypt disks")),
        ),
        label=_("Encrypted ZFS volume?"),
        widget=forms.RadioSelect(),
        initial="import",
    )

    def done(self, *args, **kwargs):
        # Detach all unused geli providers before proceeding
        # This makes sure do not import pools without proper key
        _notifier = notifier()
        for dev, name in notifier().geli_get_all_providers():
            try:
                _notifier.geli_detach(dev)
            except Exception as ee:
                log.warn(str(ee))


class AutoImportDecryptForm(Form):
    disks = forms.MultipleChoiceField(
        choices=(),
    )
    key = FileField(
        label=_("Encryption Key"),
    )
    passphrase = forms.CharField(
        label=_("Passphrase"),
        required=False,
        widget=forms.widgets.PasswordInput(),
    )

    def __init__(self, *args, **kwargs):
        super(AutoImportDecryptForm, self).__init__(*args, **kwargs)
        self.fields['disks'].choices = self._populate_disk_choices()

    def _populate_disk_choices(self):
        with client as c:
            return [
                (i['dev'], i['name'])
                for i in c.call('disk.get_encrypted', {'unused': True})
            ]

    def clean(self):
        key = self.cleaned_data.get("key")
        if not key:
            return self.cleaned_data

        disks = self.cleaned_data.get("disks")
        if not disks:
            return self.cleaned_data

        passphrase = self.cleaned_data.get("passphrase")

        try:
            upload_job_and_wait(key, 'disk.decrypt', disks, passphrase)
        except JobFailed as e:
            self._errors['__all__'] = self.error_class([e.value])
        except JobAborted:
            self._errors['__all__'] = self.error_class([_('Decrypt job aborted')])

        return self.cleaned_data


class VolumeAutoImportForm(Form):

    volume_id = forms.ChoiceField(
        choices=(),
        widget=forms.Select(attrs=attrs_dict),
        label=_('Volume'))

    def __init__(self, *args, **kwargs):
        super(VolumeAutoImportForm, self).__init__(*args, **kwargs)
        self.fields['volume_id'].choices = self._volume_choices()

    @classmethod
    def _volume_choices(cls):
        volchoices = {}
        with client as c:
            for p in c.call('pool.import_find'):
                volchoices[f'{p["name"]}|{p["guid"]}'] = f'{p["name"]} [id={p["guid"]}]'
        return list(volchoices.items())

    def clean(self):
        cleaned_data = self.cleaned_data
        with client as c:
            pools = c.call('pool.import_find')
        volume_name, guid = cleaned_data.get('volume_id', '|').split('|', 1)
        for pool in pools:
            if pool['name'] == volume_name:
                if (guid and guid == pool['guid']) or not guid:
                    cleaned_data['volume'] = pool
                    break

        if cleaned_data.get('volume', None) is None:
            self._errors['__all__'] = self.error_class([
                _("You must select a volume."),
            ])

        else:
            if models.Volume.objects.filter(
                    vol_name=cleaned_data['volume']['name']).count() > 0:
                msg = _("You already have a volume with same name")
                self._errors["volume_id"] = self.error_class([msg])
                del cleaned_data["volume_id"]

        return cleaned_data


class DiskFormPartial(ModelForm):
    disk_passwd2 = forms.CharField(
        max_length=50,
        label=_("Confirm SED Password"),
        widget=forms.widgets.PasswordInput(),
        required=False,
    )

    class Meta:
        model = models.Disk
        widgets = {
            'disk_passwd': forms.widgets.PasswordInput(render_value=False)
        }
        exclude = (
            'disk_transfermode',  # This option isn't used anywhere
        )

    def __init__(self, *args, **kwargs):
        super(DiskFormPartial, self).__init__(*args, **kwargs)
        instance = getattr(self, 'instance', None)
        if instance and instance.pk:
            self._original_smart_en = self.instance.disk_togglesmart
            self._original_smart_opts = self.instance.disk_smartoptions
            self.fields['disk_name'].widget.attrs['readonly'] = True
            self.fields['disk_name'].widget.attrs['class'] = (
                'dijitDisabled dijitTextBoxDisabled '
                'dijitValidationTextBoxDisabled')
            self.fields['disk_serial'].widget.attrs['readonly'] = True
            self.fields['disk_serial'].widget.attrs['class'] = (
                'dijitDisabled dijitTextBoxDisabled '
                'dijitValidationTextBoxDisabled')

    def clean_disk_name(self):
        return self.instance.disk_name

    def clean_disk_passwd2(self):
        password1 = self.cleaned_data.get("disk_passwd")
        password2 = self.cleaned_data.get("disk_passwd2")
        if password1 != password2:
            raise forms.ValidationError(_("The two password fields didn't match."))
        return password2

    def clean(self):
            cdata = self.cleaned_data
            if not cdata.get("disk_passwd"):
                cdata['disk_passwd'] = self.instance.disk_passwd
            return cdata

    def save(self, *args, **kwargs):
        obj = super(DiskFormPartial, self).save(*args, **kwargs)
        # Commit ataidle changes, if any
        if (
            obj.disk_hddstandby != obj._original_state['disk_hddstandby'] or
            obj.disk_advpowermgmt != obj._original_state['disk_advpowermgmt'] or
            obj.disk_acousticlevel != obj._original_state['disk_acousticlevel']
        ):
            notifier().start_ataidle(obj.disk_name)

        if (
            obj.disk_togglesmart != self._original_smart_en or
            obj.disk_smartoptions != self._original_smart_opts
        ):
            with client as c:
                if obj.disk_togglesmart == 0:
                    c.call('disk.toggle_smart_off', obj.disk_name)
                else:
                    c.call('disk.toggle_smart_on', obj.disk_name)
            started = notifier().restart("smartd")
            if (
                started is False and
                services.objects.get(srv_service='smartd').srv_enable
            ):
                raise ServiceFailed(
                    "smartd",
                    _("The SMART service failed to restart.")
                )
        return obj


class DiskEditBulkForm(Form):

    ids = forms.CharField(
        widget=forms.widgets.HiddenInput(),
    )
    disk_hddstandby = forms.ChoiceField(
        choices=(('', '-----'),) + choices.HDDSTANDBY_CHOICES,
        required=False,
        initial="Always On",
        label=_("HDD Standby")
    )
    disk_advpowermgmt = forms.ChoiceField(
        required=False,
        choices=(('', '-----'),) + choices.ADVPOWERMGMT_CHOICES,
        label=_("Advanced Power Management")
    )
    disk_acousticlevel = forms.ChoiceField(
        required=False,
        choices=(('', '-----'),) + choices.ACOUSTICLVL_CHOICES,
        label=_("Acoustic Level")
    )
    disk_togglesmart = forms.BooleanField(
        initial=True,
        label=_("Enable S.M.A.R.T."),
        required=False,
    )
    disk_smartoptions = forms.CharField(
        max_length=120,
        label=_("S.M.A.R.T. extra options"),
        required=False,
    )

    def __init__(self, *args, **kwargs):
        self._disks = kwargs.pop('disks')
        super(DiskEditBulkForm, self).__init__(*args, **kwargs)
        self.fields['ids'].initial = ','.join([str(d.pk) for d in self._disks])

        """
        Make sure all the disks have a same option for each field
        If they are not default to empty.
        """
        initials = {}
        for disk in self._disks:

            for opt in (
                'disk_hddstandby',
                'disk_advpowermgmt',
                'disk_acousticlevel',
                'disk_smartoptions',
            ):
                if opt not in initials:
                    initials[opt] = getattr(disk, opt)
                elif initials[opt] != getattr(disk, opt):
                    initials[opt] = ''

            if 'disk_togglesmart' not in initials:
                initials['disk_togglesmart'] = disk.disk_togglesmart
            elif initials['disk_togglesmart'] != disk.disk_togglesmart:
                initials['disk_togglesmart'] = True

        for key, val in list(initials.items()):
            self.fields[key].initial = val

    def save(self):
        for disk in self._disks:

            for opt in (
                'disk_hddstandby',
                'disk_advpowermgmt',
                'disk_acousticlevel',
            ):
                if self.cleaned_data.get(opt):
                    setattr(disk, opt, self.cleaned_data.get(opt))

            disk.disk_togglesmart = self.cleaned_data.get(
                "disk_togglesmart")
            # This is not a choice field, an empty value should reset all
            disk.disk_smartoptions = self.cleaned_data.get(
                "disk_smartoptions")
            disk.save()
        return self._disks


class ZFSDatasetCommonForm(Form):
    dataset_comments = forms.CharField(
        max_length=1024,
        label=_('Comments'),
        required=False)
    dataset_sync = forms.ChoiceField(
        choices=choices.ZFS_SyncChoices,
        widget=forms.Select(attrs=attrs_dict),
        label=_('Sync'),
        initial=choices.ZFS_SyncChoices[0][0])
    dataset_compression = forms.ChoiceField(
        choices=choices.ZFS_CompressionChoices,
        widget=forms.Select(attrs=attrs_dict),
        label=_('Compression level'),
        initial=choices.ZFS_CompressionChoices[0][0])
    dataset_share_type = forms.ChoiceField(
        choices=choices.SHARE_TYPE_CHOICES,
        widget=forms.Select(attrs=attrs_dict),
        label=_('Share type'),
        initial=choices.SHARE_TYPE_CHOICES[0][0])
    dataset_atime = forms.ChoiceField(
        choices=choices.ZFS_AtimeChoices,
        widget=forms.RadioSelect(attrs=attrs_dict),
        label=_('Enable atime'),
        initial=choices.ZFS_AtimeChoices[0][0])
    dataset_refquota = forms.CharField(
        max_length=128,
        initial=0,
        label=_('Quota for this dataset'),
        help_text=_('0=Unlimited; example: 1 GiB'))
    dataset_quota = forms.CharField(
        max_length=128,
        initial=0,
        label=_('Quota for this dataset and all children'),
        help_text=_('0=Unlimited; example: 1 GiB'))
    dataset_refreservation = forms.CharField(
        max_length=128,
        initial=0,
        label=_('Reserved space for this dataset'),
        help_text=_('0=None; example: 1 GiB'))
    dataset_reservation = forms.CharField(
        max_length=128,
        initial=0,
        label=_('Reserved space for this dataset and all children'),
        help_text=_('0=None; example: 1 GiB'))
    dataset_dedup = forms.ChoiceField(
        label=_('ZFS Deduplication'),
        choices=choices.ZFS_DEDUP_INHERIT,
        widget=WarningSelect(text=DEDUP_WARNING),
        initial="inherit",
    )
    dataset_readonly = forms.ChoiceField(
        label=_('Read-Only'),
        choices=choices.ZFS_ReadonlyChoices,
        initial=choices.ZFS_ReadonlyChoices[0][0],
    )
    dataset_exec = forms.ChoiceField(
        label=_('Exec'),
        choices=choices.ZFS_ExecChoices,
        initial=choices.ZFS_ExecChoices[0][0],
    )
    dataset_recordsize = forms.ChoiceField(
        choices=(('inherit', _('Inherit')), ) + choices.ZFS_RECORDSIZE,
        label=_('Record Size'),
        initial="",
        required=False,
        help_text=_(
            "Specifies a suggested block size for files in the file system. "
            "This property is designed solely for use with database workloads "
            "that access files in fixed-size records.  ZFS automatically tunes"
            " block sizes according to internal algorithms optimized for "
            "typical access patterns."
        )
    )

    advanced_fields = (
        'dataset_readonly',
        'dataset_refquota',
        'dataset_quota',
        'dataset_refreservation',
        'dataset_reservation',
        'dataset_recordsize',
        'dataset_exec',
    )

    zfs_size_fields = ['quota', 'refquota', 'reservation', 'refreservation']

    def __init__(self, *args, fs=None, **kwargs):
        self._fs = fs
        super(ZFSDatasetCommonForm, self).__init__(*args, **kwargs)

        if hasattr(self, 'parentdata'):
            self.fields['dataset_atime'].choices = _inherit_choices(
                choices.ZFS_AtimeChoices,
                self.parentdata['atime'][0]
            )
            self.fields['dataset_sync'].choices = _inherit_choices(
                choices.ZFS_SyncChoices,
                self.parentdata['sync'][0]
            )
            self.fields['dataset_compression'].choices = _inherit_choices(
                choices.ZFS_CompressionChoices,
                self.parentdata['compression'][0]
            )
            self.fields['dataset_dedup'].choices = _inherit_choices(
                choices.ZFS_DEDUP_INHERIT,
                self.parentdata['dedup'][0]
            )
            self.fields['dataset_readonly'].choices = _inherit_choices(
                choices.ZFS_ReadonlyChoices,
                self.parentdata['readonly'][0]
            )
            self.fields['dataset_exec'].choices = _inherit_choices(
                choices.ZFS_ExecChoices,
                self.parentdata['exec'][0]
            )

        if not dedup_enabled():
            self.fields['dataset_dedup'].widget.attrs['readonly'] = True
            self.fields['dataset_dedup'].widget.attrs['class'] = (
                'dijitSelectDisabled dijitDisabled')
            self.fields['dataset_dedup'].widget.text = mark_safe(
                '<span style="color: red;">Dedup feature not activated. '
                'Contact <a href="mailto:truenas-support@ixsystems.com?subject'
                '=ZFS Deduplication Activation">TrueNAS Support</a> for '
                'assistance.</span><br />'
            )

    def clean_dataset_name(self):
        name = self.cleaned_data["dataset_name"]
        if not re.search(r'^[a-zA-Z0-9][a-zA-Z0-9_\-:. ]*$', name):
            raise forms.ValidationError(_(
                "Dataset names must begin with an "
                "alphanumeric character and may only contain "
                "\"-\", \"_\", \":\", \" \" and \".\"."))

        return name

    def clean_data_to_props(self):
        props = dict()
        for prop in self.zfs_size_fields:
            value = self.cleaned_data.get('dataset_%s' % prop)
            if value and value == '0':
                value = 'none'

            props[prop] = value

        for prop in (
            'org.freenas:description', 'sync', 'compression', 'atime', 'dedup',
            'aclmode', 'recordsize', 'casesensitivity', 'readonly', 'exec',
        ):
            if prop == 'org.freenas:description':
                value = self.cleaned_data.get('dataset_comments')
            elif prop == 'recordsize':
                value = self.clean_dataset_recordsize()
            elif prop == 'casesensitivity':
                value = self.cleaned_data.get('dataset_case_sensitivity')
            else:
                value = self.cleaned_data.get('dataset_%s' % prop)

            if value:
                props[prop] = value

        return props

    def clean_dataset_recordsize(self):
        rs = self.cleaned_data.get("dataset_recordsize")
        if not rs or rs == 'inherit':
            return 'inherit'

        try:
            return int(rs)
        except ValueError:
            if rs[-1].lower() == 'k':
                rs = int(rs[:-1]) * 1024
                return rs

        raise forms.ValidationError(_('invalid recordsize provided'))


class ZFSDatasetCreateForm(ZFSDatasetCommonForm):
    dataset_name = forms.CharField(
        max_length=128,
        label=_('Dataset Name'))
    dataset_case_sensitivity = forms.ChoiceField(
        choices=choices.CASE_SENSITIVITY_CHOICES,
        initial=choices.CASE_SENSITIVITY_CHOICES[0][0],
        widget=forms.Select(attrs=attrs_dict),
        label=_('Case Sensitivity'))

    field_order = ['dataset_name']

    def __init__(self, *args, fs=None, **kwargs):
        # Common form expects a parentdata
        # We use `fs` as parent data because thats where we inherit props from
        self.parentdata = notifier().zfs_get_options(fs)
        super(ZFSDatasetCreateForm, self).__init__(*args, fs=fs, **kwargs)

    def clean(self):
        cleaned_data = _clean_zfssize_fields(
            self,
            self.zfs_size_fields,
            'dataset_')

        full_dataset_name = "%s/%s" % (self._fs, cleaned_data.get("dataset_name"))

        path = f'/mnt/{full_dataset_name}'
        if os.path.exists(path):
            raise forms.ValidationError(_('The path %s already exists.') % path)

        if len(zfs.list_datasets(path=full_dataset_name)) > 0:
            msg = _("You already have a dataset with the same name")
            self._errors["dataset_name"] = self.error_class([msg])
            del cleaned_data["dataset_name"]

        return cleaned_data

    def save(self):
        props = self.clean_data_to_props()
        path = '%s/%s' % (self._fs, self.cleaned_data.get('dataset_name'))

        err, msg = notifier().create_zfs_dataset(path=path, props=props)
        if err:
            self._errors['__all__'] = self.error_class([msg])
            return False

        notifier().change_dataset_share_type(path, self.cleaned_data.get('dataset_share_type'))

        return True


class ZFSDatasetEditForm(ZFSDatasetCommonForm):

    def __init__(self, *args, fs=None, **kwargs):
        # Common form expects a parentdata
        # We use parent `fs` as parent data because thats where we inherit props from
        self.parentdata = notifier().zfs_get_options(fs.rsplit('/', 1)[0])
        super(ZFSDatasetEditForm, self).__init__(*args, fs=fs, **kwargs)

        zdata = notifier().zfs_get_options(self._fs)

        if 'org.freenas:description' in zdata and zdata['org.freenas:description'][2] == 'local':
            self.fields['dataset_comments'].initial = zdata['org.freenas:description'][0]

        for k, v in self.get_initial_data(self._fs).items():
            self.fields[k].initial = v

    @classmethod
    def get_initial_data(cls, fs):
        """
        Method to get initial data for the form.
        This is a separate method to share with API code.
        """
        zdata = notifier().zfs_get_options(fs)
        data = {}

        if 'org.freenas:description' in zdata and zdata['org.freenas:description'][2] == 'local':
            data['dataset_comments'] = zdata['org.freenas:description'][0]
        for prop in cls.zfs_size_fields:
            field_name = f'dataset_{prop}'
            if zdata[prop][0] == '0' or zdata[prop][0] == 'none':
                data[field_name] = 0
            else:
                if zdata[prop][2] == 'local':
                    data[field_name] = zdata[prop][0]

        if zdata['dedup'][2] == 'inherit':
            data['dataset_dedup'] = 'inherit'
        elif zdata['dedup'][0] in ('on', 'off', 'verify'):
            data['dataset_dedup'] = zdata['dedup'][0]
        elif zdata['dedup'][0] == 'sha256,verify':
            data['dataset_dedup'] = 'verify'
        else:
            data['dataset_dedup'] = 'off'

        if zdata['sync'][2] == 'inherit':
            data['dataset_sync'] = 'inherit'
        else:
            data['dataset_sync'] = zdata['sync'][0]

        if zdata['compression'][2] == 'inherit':
            data['dataset_compression'] = 'inherit'
        else:
            data['dataset_compression'] = zdata['compression'][0]

        if zdata['atime'][2] == 'inherit':
            data['dataset_atime'] = 'inherit'
        elif zdata['atime'][0] in ('on', 'off'):
            data['dataset_atime'] = zdata['atime'][0]
        else:
            data['dataset_atime'] = 'off'

        if zdata['readonly'][2] == 'inherit':
            data['dataset_readonly'] = 'inherit'
        elif zdata['readonly'][0] in ('on', 'off'):
            data['dataset_readonly'] = zdata['readonly'][0]
        else:
            data['dataset_readonly'] = 'off'

        if zdata['exec'][2] == 'inherit':
            data['dataset_exec'] = 'inherit'
        elif zdata['exec'][0] in ('on', 'off'):
            data['dataset_exec'] = zdata['exec'][0]
        else:
            data['dataset_exec'] = 'off'

        if zdata['recordsize'][2] == 'inherit':
            data['dataset_recordsize'] = 'inherit'
        else:
            data['dataset_recordsize'] = zdata['recordsize'][0]

        data['dataset_share_type'] = notifier().get_dataset_share_type(fs)

        return data

    def clean(self):
        cleaned_data = _clean_zfssize_fields(
            self,
            self.zfs_size_fields,
            'dataset_')

        return cleaned_data

    def save(self):
        props = self.clean_data_to_props()
        name = self._fs

        error = False
        errors = dict()

        for item, value in props.items():
            if value == 'inherit':
                success, msg = notifier().zfs_inherit_option(name, item)
            else:
                success, msg = notifier().zfs_set_option(name, item, value)

            error |= not success
            if not success:
                error = True
                errors[f'dataset_{item}'] = msg

        notifier().change_dataset_share_type(name, self.cleaned_data.get('dataset_share_type'))

        for field, err in list(errors.items()):
            self._errors[field] = self.error_class([err])

        if error:
            return False

        return True


class CommonZVol(Form):
    zvol_comments = forms.CharField(max_length=120, label=_('Comments'), required=False)
    zvol_volsize = forms.CharField(
        max_length=128,
        label=_('Size for this zvol'),
        help_text=_('Example: 1 GiB'),
    )
    zvol_force = forms.BooleanField(
        label=_('Force size'),
        required=False,
        help_text=_('Allow the zvol to consume more than 80% of available space'),
    )
    zvol_sync = forms.ChoiceField(
        choices=choices.ZFS_SyncChoices,
        initial='inherit',
        widget=forms.Select(attrs=attrs_dict),
        label=_('Sync'))
    zvol_compression = forms.ChoiceField(
        choices=choices.ZFS_CompressionChoices,
        initial='inherit',
        widget=forms.Select(attrs=attrs_dict),
        label=_('Compression level'))
    zvol_dedup = forms.ChoiceField(
        label=_('ZFS Deduplication'),
        choices=choices.ZFS_DEDUP_INHERIT,
        initial='inherit',
        widget=WarningSelect(text=DEDUP_WARNING),
    )

    def __init__(self, *args, **kwargs):
        self._force = False
        super(CommonZVol, self).__init__(*args, **kwargs)

        if hasattr(self, 'parentdata'):
            self.fields['zvol_sync'].choices = _inherit_choices(
                choices.ZFS_SyncChoices,
                self.parentdata['sync'][0]
            )
            self.fields['zvol_compression'].choices = _inherit_choices(
                choices.ZFS_CompressionChoices,
                self.parentdata['compression'][0]
            )
            self.fields['zvol_dedup'].choices = _inherit_choices(
                choices.ZFS_DEDUP_INHERIT,
                self.parentdata['dedup'][0]
            )

    def _zvol_force(self):
        if self._force:
            if not self.cleaned_data.get('zvol_force'):
                self._errors['zvol_volsize'] = self.error_class([
                    'It is not recommended to use more than 80% of your '
                    'available space for your zvol!'
                ])

    def clean_zvol_volsize(self):
        size = self.cleaned_data.get('zvol_volsize').replace(' ', '')
        reg = re.search(r'^(\d+(?:\.\d+)?)([BKMGTP](?:iB)?)$', size, re.I)
        if not reg:
            raise forms.ValidationError(
                _('Specify the size with IEC suffixes, e.g. 10 GiB')
            )

        number, suffix = reg.groups()
        if suffix.lower().endswith('ib'):
            size = '%s%s' % (number, suffix[0])

        zlist = zfs.zfs_list(path=self.parentds, include_root=True, recursive=True, hierarchical=False)
        if zlist:
            dataset = zlist.get(self.parentds)
            _map = {
                'P': 1125899906842624,
                'T': 1099511627776,
                'G': 1073741824,
                'M': 1048576,
            }
            if suffix in _map:
                cmpsize = Decimal(number) * _map.get(suffix)
            else:
                cmpsize = Decimal(number)
            avail = dataset.avail
            if hasattr(self, 'name'):
                zvol = zlist.get(self.name)
                if zvol:
                    avail += zvol.used
            if cmpsize > avail * 0.80:
                self._force = True

        return size


class ZVol_EditForm(CommonZVol):

    def __init__(self, *args, **kwargs):
        # parentds is required for CommonZVol
        self.name = kwargs.pop('name')
        self.parentds = self.name.rsplit('/', 1)[0]
        _n = notifier()
        self.parentdata = _n.zfs_get_options(self.parentds)
        super(ZVol_EditForm, self).__init__(*args, **kwargs)

        self.zdata = _n.zfs_get_options(self.name)
        if 'org.freenas:description' in self.zdata and self.zdata['org.freenas:description'][2] == 'local':
            self.fields['zvol_comments'].initial = self.zdata['org.freenas:description'][0]
        if self.zdata['sync'][2] == 'inherit':
            self.fields['zvol_sync'].initial = 'inherit'
        else:
            self.fields['zvol_sync'].initial = self.zdata['sync'][0]
        if self.zdata['compression'][2] == 'inherit':
            self.fields['zvol_compression'].initial = 'inherit'
        else:
            self.fields['zvol_compression'].initial = self.zdata['compression'][0]
        self.fields['zvol_volsize'].initial = self.zdata['volsize'][0]

        if self.zdata['dedup'][2] == 'inherit':
            self.fields['zvol_dedup'].initial = 'inherit'
        elif self.zdata['dedup'][0] in ('on', 'off', 'verify'):
            self.fields['zvol_dedup'].initial = self.zdata['dedup'][0]
        elif self.zdata['dedup'][0] == 'sha256,verify':
            self.fields['zvol_dedup'].initial = 'verify'
        else:
            self.fields['zvol_dedup'].initial = 'off'

        if not dedup_enabled():
            self.fields['zvol_dedup'].widget.attrs['readonly'] = True
            self.fields['zvol_dedup'].widget.attrs['class'] = (
                'dijitSelectDisabled dijitDisabled')
            self.fields['zvol_dedup'].widget.text = mark_safe(
                '<span style="color: red;">Dedup feature not activated. '
                'Contact <a href="mailto:truenas-support@ixsystems.com?subject'
                '=ZFS Deduplication Activation">TrueNAS Support</a> for '
                'assistance.</span><br />'
            )

    def clean(self):
        cleaned_data = _clean_zfssize_fields(self, ('volsize', ), "zvol_")
        volsize = cleaned_data.get('zvol_volsize')
        if volsize and 'zvol_volsize' not in self._errors:
            if humansize_to_bytes(self.zdata['volsize'][0]) > humansize_to_bytes(volsize):
                self._errors['zvol_volsize'] = self.error_class([
                    _('You cannot shrink a zvol from GUI, this may lead to data loss.')
                ])
        self._zvol_force()
        return cleaned_data

    def save(self):
        _n = notifier()
        error = False
        for attr, formfield, can_inherit in (
            ('org.freenas:description', 'zvol_comments', False),
            ('sync', None, True),
            ('compression', None, True),
            ('dedup', None, True),
            ('volsize', None, True),
        ):
            if not formfield:
                formfield = f'zvol_{attr}'
            if can_inherit and self.cleaned_data[formfield] == 'inherit':
                success, err = _n.zfs_inherit_option(self.name, attr)
            else:
                success, err = _n.zfs_set_option(
                    self.name, attr, self.cleaned_data[formfield]
                )
            if not success:
                error = True
                self._errors[formfield] = self.error_class([err])

        if error:
            return False
        extents = iSCSITargetExtent.objects.filter(
            iscsi_target_extent_type='ZVOL',
            iscsi_target_extent_path=f'zvol/{self.parentds}')
        if extents.exists():
            _n.reload('iscsitarget')
        return True


class ZVol_CreateForm(CommonZVol):
    zvol_name = forms.CharField(max_length=128, label=_('zvol name'))
    zvol_sparse = forms.BooleanField(
        label=_('Sparse volume'),
        help_text=_(
            'Creates a sparse volume with no reservation, also known '
            'as "thin provisioning". A "sparse volume" is a volume where the '
            'reservation is less than the volume size. Consequently, writes '
            'to a sparse volume can fail with ENOSPC when the pool is low on '
            'space. (NOT RECOMMENDED)'),
        required=False,
        initial=False,
    )
    zvol_blocksize = forms.ChoiceField(
        label=_('Block size'),
        help_text=_(
            'The default of the zvol block size is chosen automatically based '
            'on the number of the disks in the pool for a general use case.'
        ),
        required=False,
        choices=(('', _('Inherit')), ) + choices.ZFS_VOLBLOCKSIZE,
    )

    advanced_fields = (
        'zvol_blocksize',
    )

    def __init__(self, *args, **kwargs):
        self.parentds = kwargs.pop('parentds')
        self.parentdata = notifier().zfs_get_options(self.parentds)
        zpool = notifier().zpool_parse(self.parentds.split('/')[0])
        numdisks = 4
        for vdev in zpool.data:
            if vdev.type in (
                'cache',
                'spare',
                'log',
                'log mirror',
            ):
                continue
            if vdev.type == 'raidz':
                num = len(list(iter(vdev))) - 1
            elif vdev.type == 'raidz2':
                num = len(list(iter(vdev))) - 2
            elif vdev.type == 'raidz3':
                num = len(list(iter(vdev))) - 3
            elif vdev.type == 'mirror':
                num = 1
            else:
                num = len(list(iter(vdev)))
            if num > numdisks:
                numdisks = num
        super(ZVol_CreateForm, self).__init__(*args, **kwargs)
        key_order(self, 0, 'zvol_name', instance=True)
        size = '%dK' % 2 ** ((numdisks * 4) - 1).bit_length()

        if size in [y[0] for y in choices.ZFS_VOLBLOCKSIZE]:
            self.fields['zvol_blocksize'].initial = size

    def clean_zvol_name(self):
        name = self.cleaned_data["zvol_name"]
        if not re.search(r'^[a-zA-Z0-9][a-zA-Z0-9_\-:.]*$', name):
            raise forms.ValidationError(_(
                "ZFS Volume names must begin with "
                "an alphanumeric character and may only contain "
                "(-), (_), (:) and (.)."))
        return name

    def clean(self):
        cleaned_data = self.cleaned_data
        full_zvol_name = "%s/%s" % (
            self.parentds,
            cleaned_data.get("zvol_name"))
        if len(zfs.list_datasets(path=full_zvol_name)) > 0:
            msg = _("You already have a dataset with the same name")
            self._errors["zvol_name"] = self.error_class([msg])
            del cleaned_data["zvol_name"]

        self._zvol_force()
        return cleaned_data

    def save(self):
        props = {}
        zvol_volsize = self.cleaned_data.get('zvol_volsize')
        zvol_blocksize = self.cleaned_data.get("zvol_blocksize")
        zvol_name = f"{self.parentds}/{self.cleaned_data.get('zvol_name')}"
        zvol_comments = self.cleaned_data.get('zvol_comments')
        zvol_sync = self.cleaned_data.get('zvol_sync')
        zvol_compression = self.cleaned_data.get('zvol_compression')
        props['sync'] = str(zvol_sync)
        props['compression'] = str(zvol_compression)
        if zvol_blocksize:
            props['volblocksize'] = zvol_blocksize
        errno, errmsg = notifier().create_zfs_vol(
            name=str(zvol_name),
            size=str(zvol_volsize),
            sparse=self.cleaned_data.get("zvol_sparse", False),
            props=props)
        notifier().zfs_set_option(name=str(zvol_name), item="org.freenas:description", value=zvol_comments)
        if errno != 0:
            self._errors['__all__'] = self.error_class([errmsg])
            return False
        return True


class MountPointAccessForm(Form):
    mp_user_en = forms.BooleanField(
        label=_('Apply Owner (user)'),
        initial=True,
        required=False,
    )
    mp_user = UserField(label=_('Owner (user)'))
    mp_group_en = forms.BooleanField(
        label=_('Apply Owner (group)'),
        initial=True,
        required=False,
    )
    mp_group = GroupField(label=_('Owner (group)'))
    mp_mode_en = forms.BooleanField(
        label=_('Apply Mode'),
        initial=True,
        required=False,
    )
    mp_mode = UnixPermissionField(label=_('Mode'), required=False)
    mp_acl = forms.ChoiceField(
        label=_('Permission Type'),
        choices=(
            ('unix', 'Unix'),
            ('mac', 'Mac'),
            ('windows', 'Windows'),
        ),
        initial='unix',
        widget=forms.widgets.RadioSelect(),
    )
    mp_recursive = forms.BooleanField(
        initial=False,
        required=False,
        label=_('Set permission recursively')
    )

    def __init__(self, *args, **kwargs):
        super(MountPointAccessForm, self).__init__(*args, **kwargs)

        path = kwargs.get('initial', {}).get('path', None)
        if path:
            if os.path.exists(os.path.join(path, ".windows")):
                self.fields['mp_acl'].initial = 'windows'
                self.fields['mp_mode'].widget.attrs['disabled'] = 'disabled'
            elif os.path.exists(os.path.join(path, ".mac")):
                self.fields['mp_acl'].initial = 'mac'
            else:
                self.fields['mp_acl'].initial = 'unix'
            # 8917: This needs to be handled by an upper layer but for now
            # just prevent a backtrace.
            try:
                self.fields['mp_mode'].initial = "%.3o" % (
                    notifier().mp_get_permission(path),
                )
                user, group = notifier().mp_get_owner(path)
                self.fields['mp_user'].initial = user
                self.fields['mp_group'].initial = group
            except Exception:
                pass
        self.fields['mp_acl'].widget.attrs['onChange'] = "mpAclChange(this);"

    def clean(self):
        if (
            (self.cleaned_data.get("mp_acl") == "unix" or
                self.cleaned_data.get("mp_acl") == "mac") and not
                self.cleaned_data.get("mp_mode")
        ):
            self._errors['mp_mode'] = self.error_class([
                _("This field is required")
            ])
        return self.cleaned_data

    def commit(self, path='/mnt/'):

        kwargs = {}

        if self.cleaned_data.get('mp_group_en'):
            kwargs['group'] = self.cleaned_data['mp_group']

        if self.cleaned_data.get('mp_mode_en'):
            kwargs['mode'] = str(self.cleaned_data['mp_mode'])

        if self.cleaned_data.get('mp_user_en'):
            kwargs['user'] = self.cleaned_data['mp_user']

        notifier().mp_change_permission(
            path=path,
            recursive=self.cleaned_data['mp_recursive'],
            acl=self.cleaned_data['mp_acl'],
            **kwargs
        )


class ResilverForm(MiddlewareModelForm, ModelForm):

    middleware_attr_schema = 'pool_resilver'
    middleware_attr_prefix = ''
    middleware_plugin = 'pool.resilver'
    is_singletone = True

    class Meta:
        fields = '__all__'
        model = models.Resilver
        widgets = {
            'weekday': CheckboxSelectMultiple(
                choices=choices.WEEKDAYS_CHOICES
            ),
        }

    def __init__(self, *args, **kwargs):
        if len(args) > 0 and isinstance(args[0], QueryDict):
            new = args[0].copy()
            fix_time_fields(new, ['begin', 'end'])
            args = (new,) + args[1:]
        super(ResilverForm, self).__init__(*args, **kwargs)

    def clean_weekday(self):
        return self.data.getlist('weekday')

    def clean_begin(self):
        begin = self.data.get('begin')
        return begin.strftime('%H:%M')

    def clean_end(self):
        end = self.data.get('end')
        return end.strftime('%H:%M')


class PeriodicSnapForm(ModelForm):

    class Meta:
        fields = '__all__'
        model = models.Task
        widgets = {
            'task_byweekday': CheckboxSelectMultiple(
                choices=choices.WEEKDAYS_CHOICES),
            'task_begin': forms.widgets.TimeInput(attrs={
                'constraints': mark_safe("{timePattern:'HH:mm:ss',}"),
            }),
            'task_end': forms.widgets.TimeInput(attrs={
                'constraints': mark_safe("{timePattern:'HH:mm:ss',}"),
            }),
        }

    def __init__(self, *args, **kwargs):
        if len(args) > 0 and isinstance(args[0], QueryDict):
            new = args[0].copy()
            fix_time_fields(new, ['task_begin', 'task_end'])
            args = (new,) + args[1:]
        super(PeriodicSnapForm, self).__init__(*args, **kwargs)
        self.fields['task_filesystem'] = forms.ChoiceField(
            label=self.fields['task_filesystem'].label,
        )
        volnames = [o.vol_name for o in models.Volume.objects.all()]
        choices = set([y for y in list(notifier().list_zfs_fsvols().items()) if y[0].split('/')[0] in volnames])
        if self.instance.id:
            choices.add((self.instance.task_filesystem, self.instance.task_filesystem))
        self.fields['task_filesystem'].choices = list(choices)
        self.fields['task_repeat_unit'].widget = forms.HiddenInput()

    def clean_task_byweekday(self):
        bwd = self.data.getlist('task_byweekday')
        return ','.join(bwd)

    def clean(self):
        cdata = self.cleaned_data
        if cdata['task_repeat_unit'] == 'weekly' and \
                len(cdata['task_byweekday']) == 0:
            self._errors['task_byweekday'] = self.error_class([
                _("At least one day must be chosen"),
            ])
            del cdata['task_byweekday']
        return cdata


class ManualSnapshotForm(Form):
    ms_recursively = forms.BooleanField(
        initial=False,
        required=False,
        label=_('Recursive snapshot'))
    ms_name = forms.CharField(label=_('Snapshot Name'))

    def __init__(self, *args, **kwargs):
        self._fs = kwargs.pop('fs', None)
        super(ManualSnapshotForm, self).__init__(*args, **kwargs)
        self.fields['ms_name'].initial = datetime.today().strftime(
            'manual-%Y%m%d')

        if models.VMWarePlugin.objects.filter(filesystem=self._fs).exists():
            self.fields['vmwaresync'] = forms.BooleanField(
                required=False,
                label=_('VMware Sync'),
                initial=True,
            )

    def clean_ms_name(self):
        regex = re.compile('^[-a-zA-Z0-9_. ]+$')
        name = self.cleaned_data.get('ms_name')
        if regex.match(name) is None:
            raise forms.ValidationError(
                _("Only [-a-zA-Z0-9_. ] permitted as snapshot name")
            )
        snaps = notifier().zfs_snapshot_list(path=f'{self._fs}@{name}')
        if snaps:
            raise forms.ValidationError(
                _('Snapshot with this name already exists')
            )
        return name

    def commit(self, fs):
        vmsnapname = str(uuid.uuid4())
        vmsnapdescription = str(datetime.now()).split('.')[0] + " FreeNAS Created Snapshot"
        snapvms = []
        for obj in models.VMWarePlugin.objects.filter(filesystem=self._fs):
            try:
                ssl_context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
                ssl_context.verify_mode = ssl.CERT_NONE

                si = connect.SmartConnect(host=obj.hostname, user=obj.username, pwd=obj.get_password(), sslContext=ssl_context)
            except Exception:
                continue
            content = si.RetrieveContent()
            vm_view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
            for vm in vm_view.view:
                if vm.summary.runtime.powerState != 'poweredOn':
                    continue
                for i in vm.datastore:
                    if i.info.name == obj.datastore:
                        VimTask.WaitForTask(vm.CreateSnapshot_Task(
                            name=vmsnapname,
                            description=vmsnapdescription,
                            memory=False, quiesce=False,
                        ))
                        snapvms.append(vm)
                        break

        try:
            notifier().zfs_mksnap(
                fs,
                str(self.cleaned_data['ms_name']),
                self.cleaned_data['ms_recursively'],
                len(snapvms))
        finally:
            for vm in snapvms:
                tree = vm.snapshot.rootSnapshotList
                while tree[0].childSnapshotList is not None:
                    snap = tree[0]
                    if snap.name == vmsnapname:
                        VimTask.WaitForTask(snap.snapshot.RemoveSnapshot_Task(True))
                    if len(tree[0].childSnapshotList) < 1:
                        break
                    tree = tree[0].childSnapshotList


class CloneSnapshotForm(Form):
    cs_snapshot = forms.CharField(label=_('Snapshot'))
    cs_name = forms.CharField(label=_('Clone Name (must be on same volume)'))

    def __init__(self, *args, **kwargs):
        is_volume = kwargs.pop('is_volume', False)
        super(CloneSnapshotForm, self).__init__(*args, **kwargs)
        self.fields['cs_snapshot'].widget.attrs['readonly'] = True
        self.fields['cs_snapshot'].widget.attrs['class'] = (
            'dijitDisabled dijitTextBoxDisabled '
            'dijitValidationTextBoxDisabled')
        self.fields['cs_snapshot'].initial = kwargs['initial']['cs_snapshot']
        self.fields['cs_snapshot'].value = kwargs['initial']['cs_snapshot']
        dataset, snapname = kwargs['initial']['cs_snapshot'].split('@')
        if is_volume:
            dataset, zvol = dataset.rsplit('/', 1)
            self.fields['cs_name'].initial = '%s/%s-clone-%s' % (
                dataset,
                zvol,
                snapname)
        else:
            if '/' in dataset:
                dataset = '%s-' % dataset
            else:
                dataset = '%s/' % dataset
            self.fields['cs_name'].initial = '%s%s-clone' % (
                dataset,
                snapname)

    def clean_cs_snapshot(self):
        return self.fields['cs_snapshot'].initial

    def clean_cs_name(self):
        regex = re.compile('^[-a-zA-Z0-9_./ ]+$')
        if regex.match(self.cleaned_data['cs_name'].__str__()) is None:
            raise forms.ValidationError(
                _("Only [-a-zA-Z0-9_./ ] permitted as clone name")
            )
        if '/' in self.fields['cs_snapshot'].initial:
            volname = self.fields['cs_snapshot'].initial.split('/')[0]
        else:
            volname = self.fields['cs_snapshot'].initial.split('@')[0]
        if not self.cleaned_data['cs_name'].startswith('%s/' % (volname)):
            raise forms.ValidationError(
                _("Clone must be within the same volume")
            )
        return self.cleaned_data['cs_name']

    def commit(self):
        snapshot = self.cleaned_data['cs_snapshot'].__str__()
        retval = notifier().zfs_clonesnap(
            snapshot,
            str(self.cleaned_data['cs_name']))
        return retval


class ZFSDiskReplacementForm(Form):

    force = forms.BooleanField(
        label=_("Force"),
        required=False,
        widget=forms.widgets.HiddenInput(),
    )
    replace_disk = forms.ChoiceField(
        choices=(),
        widget=forms.Select(attrs=attrs_dict),
        label=_('Member disk'))

    def __init__(self, *args, **kwargs):
        self.volume = kwargs.pop('volume')
        self.label = kwargs.pop('label')
        disk = notifier().label_to_disk(self.label)
        if disk is None:
            disk = self.label
        self.disk = disk
        super(ZFSDiskReplacementForm, self).__init__(*args, **kwargs)

        if self.data:
            devname = self.data.get('replace_disk')
            if devname:
                if not notifier().disk_check_clean(devname):
                    self.fields['force'].widget = forms.widgets.CheckboxInput()
        if self.volume.vol_encrypt == 2:
            self.fields['pass'] = forms.CharField(
                label=_("Passphrase"),
                widget=forms.widgets.PasswordInput(),
            )
            self.fields['pass2'] = forms.CharField(
                label=_("Confirm Passphrase"),
                widget=forms.widgets.PasswordInput(),
            )
        self.fields['replace_disk'].choices = self._populate_disk_choices()
        self.fields['replace_disk'].choices.sort(
            key=lambda a: float(
                re.sub(r'^.*?([0-9]+)[^0-9]*([0-9]*).*$', r'\1.\2', a[0])
            ))

    def _populate_disk_choices(self):

        diskchoices = dict()
        used_disks = []
        for v in models.Volume.objects.all():
            used_disks.extend(v.get_disks())

        # Grab partition list
        # NOTE: This approach may fail if device nodes are not accessible.
        disks = notifier().get_disks()

        for disk in disks:
            if disk in used_disks:
                continue
            devname, capacity = disks[disk]['devname'], disks[disk]['capacity']
            capacity = humanize_number_si(int(capacity))
            diskchoices[devname] = "%s (%s)" % (devname, capacity)

        choices = list(diskchoices.items())
        choices.sort(key=lambda a: float(
            re.sub(r'^.*?([0-9]+)[^0-9]*([0-9]*).*$', r'\1.\2', a[0])
        ))
        return choices

    def clean_replace_disk(self):
        devname = self.cleaned_data.get('replace_disk')
        force = self.cleaned_data.get('force')
        if not devname:
            return devname
        if not force and not notifier().disk_check_clean(devname):
            self._errors['force'] = self.error_class([_(
                "Disk is not clear, partitions or ZFS labels were found."
            )])
        return devname

    def clean_pass2(self):
        passphrase = self.cleaned_data.get("pass")
        passphrase2 = self.cleaned_data.get("pass2")
        if passphrase != passphrase2:
            raise forms.ValidationError(
                _("Confirmation does not match passphrase")
            )
        passfile = tempfile.mktemp(dir='/tmp/')
        with open(passfile, 'w') as f:
            os.chmod(passfile, 600)
            f.write(passphrase)
        if not notifier().geli_testkey(self.volume, passphrase=passfile):
            self._errors['pass'] = self.error_class([
                _("Passphrase is not valid")
            ])
        os.unlink(passfile)
        return passphrase

    def done(self):
        devname = self.cleaned_data['replace_disk']
        passphrase = self.cleaned_data.get("pass")
        if passphrase is not None:
            passfile = tempfile.mktemp(dir='/tmp/')
            with open(passfile, 'w') as f:
                os.chmod(passfile, 600)
                f.write(passphrase)
        else:
            passfile = None

        rv = notifier().zfs_replace_disk(
            self.volume,
            self.label,
            devname,
            force=self.cleaned_data.get('force'),
            passphrase=passfile
        )
        if rv == 0:
            if (services.objects.get(srv_service='smartd').srv_enable):
                notifier().restart("smartd")
            return True
        else:
            return False


class ReplicationForm(ModelForm):
    repl_remote_mode = forms.ChoiceField(
        label=_('Setup mode'),
        choices=(
            ('MANUAL', _('Manual')),
            ('SEMIAUTOMATIC', _('Semi-automatic')),
        ),
        initial='MANUAL',
    )
    repl_remote_hostname = forms.CharField(label=_("Remote hostname"))
    repl_remote_port = forms.IntegerField(
        label=_("Remote port"),
        initial=22,
        required=False,
        widget=forms.widgets.TextInput(),
    )
    repl_remote_http_port = forms.CharField(
        label=_('Remote HTTP/HTTPS Port'),
        max_length=200,
        initial=80,
        required=False,
    )
    repl_remote_https = forms.BooleanField(
        label=_('Remote HTTPS'),
        required=False,
        initial=False,
    )
    repl_remote_token = forms.CharField(
        label=_('Remote Auth Token'),
        max_length=100,
        required=False,
        help_text=_(
            "On the remote host go to Storage -> Replication Tasks, click the "
            "Temporary Auth Token button and paste the resulting value in to "
            "this field."
        ),
    )
    repl_remote_dedicateduser_enabled = forms.BooleanField(
        label=_("Dedicated User Enabled"),
        help_text=_("If disabled then root will be used for replication."),
        required=False,
    )
    repl_remote_dedicateduser = UserField(
        label=_("Dedicated User"),
        required=False,
    )
    repl_remote_cipher = forms.ChoiceField(
        label=_("Encryption Cipher"),
        initial='standard',
        choices=choices.REPL_CIPHER,
    )
    repl_remote_hostkey = forms.CharField(
        label=_("Remote hostkey"),
        widget=forms.Textarea(),
        required=False,
    )

    class Meta:
        fields = '__all__'
        model = models.Replication
        exclude = ('repl_lastsnapshot', 'repl_remote')
        widgets = {
            'repl_begin': forms.widgets.TimeInput(attrs={
                'constraints': mark_safe("{timePattern:'HH:mm:ss',}"),
            }),
            'repl_end': forms.widgets.TimeInput(attrs={
                'constraints': mark_safe("{timePattern:'HH:mm:ss',}"),
            }),
        }

    def __init__(self, *args, **kwargs):
        if len(args) > 0 and isinstance(args[0], QueryDict):
            new = args[0].copy()
            fix_time_fields(new, ['repl_begin', 'repl_end'])
            args = (new,) + args[1:]
        repl = kwargs.get('instance', None)
        super(ReplicationForm, self).__init__(*args, **kwargs)
        self.fields['repl_filesystem'] = forms.ChoiceField(
            label=self.fields['repl_filesystem'].label,
            help_text=_(
                "This field will be empty if you have not "
                "setup a periodic snapshot task"),
        )
        fs = list(set([
            (task.task_filesystem, task.task_filesystem)
            for task in models.Task.objects.all()
        ]))
        self.fields['repl_filesystem'].choices = fs

        if not self.instance.id:
            self.fields['repl_remote_mode'].widget.attrs['onChange'] = (
                'repliRemoteMode'
            )
        else:
            del self.fields['repl_remote_mode']
            del self.fields['repl_remote_http_port']
            del self.fields['repl_remote_https']
            del self.fields['repl_remote_token']

        self.fields['repl_remote_dedicateduser_enabled'].widget.attrs[
            'onClick'
        ] = (
            'toggleGeneric("id_repl_remote_dedicateduser_enabled", '
            '["id_repl_remote_dedicateduser"], true);')

        self.fields['repl_remote_cipher'].widget.attrs['onChange'] = (
            'remoteCipherConfirm'
        )

        if repl and repl.id:
            self.fields['repl_remote_hostname'].initial = (
                repl.repl_remote.ssh_remote_hostname)
            self.fields['repl_remote_hostname'].required = False
            self.fields['repl_remote_port'].initial = (
                repl.repl_remote.ssh_remote_port)
            self.fields['repl_remote_dedicateduser_enabled'].initial = (
                repl.repl_remote.ssh_remote_dedicateduser_enabled)
            self.fields['repl_remote_dedicateduser'].initial = (
                repl.repl_remote.ssh_remote_dedicateduser)
            self.fields['repl_remote_cipher'].initial = (
                repl.repl_remote.ssh_cipher)
            self.fields['repl_remote_hostkey'].initial = (
                repl.repl_remote.ssh_remote_hostkey)
            self.fields['repl_remote_hostkey'].required = False
            if not repl.repl_remote.ssh_remote_dedicateduser_enabled:
                self.fields['repl_remote_dedicateduser'].widget.attrs[
                    'disabled'] = 'disabled'
        else:
            if not self.data.get("repl_remote_dedicateduser_enabled", False):
                self.fields['repl_remote_dedicateduser'].widget.attrs[
                    'disabled'] = 'disabled'

        self.fields['repl_remote_cipher'].widget.attrs['data-dojo-props'] = (
            mark_safe("'oldvalue': '%s'" % (
                self.fields['repl_remote_cipher'].initial,
            ))
        )

    def clean_repl_remote_port(self):
        port = self.cleaned_data.get('repl_remote_port')
        if not port:
            return 22
        return port

    def clean_repl_remote_dedicateduser(self):
        en = self.cleaned_data.get("repl_remote_dedicateduser_enabled")
        user = self.cleaned_data.get("repl_remote_dedicateduser")
        if en and user is None:
            raise forms.ValidationError("You must select a valid user")
        return user

    def _build_uri(self):
        hostname = self.cleaned_data.get('repl_remote_hostname')
        http_port = self.cleaned_data.get('repl_remote_http_port')
        https = self.cleaned_data.get('repl_remote_https')
        return 'ws{}://{}:{}/websocket'.format(
            's' if https else '',
            hostname,
            http_port,
        )

    def clean_repl_remote_token(self):
        mode = self.cleaned_data.get('repl_remote_mode')
        token = self.cleaned_data.get('repl_remote_token')
        if mode != 'SEMIAUTOMATIC':
            return token

        if not token:
            raise forms.ValidationError(_('This field is required'))

        try:
            with Client(self._build_uri()) as c:
                if not c.call('auth.token', token):
                    raise forms.ValidationError(_('Token is invalid.'))
        except forms.ValidationError:
            raise
        except Exception as e:
            raise forms.ValidationError(_('Failed to connect to remote: %s' % e))
        return token

    def clean_repl_remote_hostkey(self):
        hostkey = self.cleaned_data.get('repl_remote_hostkey')
        mode = self.cleaned_data.get('repl_remote_mode')
        if mode == 'MANUAL' and not hostkey:
            raise forms.ValidationError(_('This field is required'))
        return hostkey

    def save(self):

        mode = self.cleaned_data.get('repl_remote_mode')

        if self.instance.id is None:
            r = models.ReplRemote()
        else:
            r = self.instance.repl_remote

        r.ssh_remote_hostname = self.cleaned_data.get("repl_remote_hostname")
        r.ssh_remote_dedicateduser_enabled = self.cleaned_data.get(
            "repl_remote_dedicateduser_enabled")
        r.ssh_remote_dedicateduser = self.cleaned_data.get(
            "repl_remote_dedicateduser")
        r.ssh_cipher = self.cleaned_data.get("repl_remote_cipher")

        if mode == 'SEMIAUTOMATIC':
            try:
                with Client(self._build_uri()) as c:
                    if not c.call('auth.token', self.cleaned_data.get('repl_remote_token')):
                        raise ValueError('Invalid token')
                    with open('/data/ssh/replication.pub', 'r') as f:
                        publickey = f.read()
                    data = c.call('replication.pair', {
                        'hostname': self.cleaned_data.get("repl_remote_hostname"),
                        'public-key': publickey,
                        'user': r.ssh_remote_dedicateduser if r.ssh_remote_dedicateduser_enabled else None,
                    })
                    r.ssh_remote_port = data['ssh_port']
                    r.ssh_remote_hostkey = data['ssh_hostkey']
            except Exception as e:
                raise MiddlewareError('Failed to setup replication: %s' % e)
        else:
            r.ssh_remote_port = self.cleaned_data.get("repl_remote_port")
            r.ssh_remote_hostkey = self.cleaned_data.get("repl_remote_hostkey")
        r.save()
        notifier().reload("ssh")
        self.instance.repl_remote = r
        rv = super(ReplicationForm, self).save()
        return rv


class ReplRemoteForm(ModelForm):

    class Meta:
        fields = '__all__'
        model = models.ReplRemote

    def save(self):
        rv = super(ReplRemoteForm, self).save()
        notifier().reload("ssh")
        return rv


class VolumeExport(Form):
    mark_new = forms.BooleanField(
        required=False,
        initial=False,
        label=_("Mark the disks as new (destroy data)"),
    )

    def __init__(self, *args, **kwargs):
        self.instance = kwargs.pop('instance', None)
        services = kwargs.pop('services', {})
        super(VolumeExport, self).__init__(*args, **kwargs)
        if list(services.keys()):
            self.fields['cascade'] = forms.BooleanField(
                initial=True,
                required=False,
                label=_("Also delete the share's configuration"))


class Dataset_Destroy(Form):
    def __init__(self, *args, **kwargs):
        self.fs = kwargs.pop('fs')
        self.datasets = kwargs.pop('datasets', [])
        super(Dataset_Destroy, self).__init__(*args, **kwargs)
        snaps = notifier().zfs_snapshot_list(path=self.fs)
        if len(snaps.get(self.fs, [])) > 0:
            label = ungettext(
                "I'm aware this will destroy snapshots within this dataset",
                ("I'm aware this will destroy all child datasets and "
                    "snapshots within this dataset"),
                len(self.datasets)
            )
            self.fields['cascade'] = forms.BooleanField(
                initial=False,
                label=label)


class ZvolDestroyForm(Form):
    def __init__(self, *args, **kwargs):
        self.fs = kwargs.pop('fs')
        super(ZvolDestroyForm, self).__init__(*args, **kwargs)
        snaps = notifier().zfs_snapshot_list(path=self.fs)
        if len(snaps.get(self.fs, [])) > 0:
            label = _(
                "I'm aware this will destroy snapshots of this zvol",
            )
            self.fields['cascade'] = forms.BooleanField(
                initial=False,
                label=label)


class ScrubForm(MiddlewareModelForm, ModelForm):

    middleware_attr_prefix = 'scrub_'
    middleware_attr_schema = 'pool_scrub'
    middleware_plugin = 'pool.scrub'
    is_singletone = False
    middleware_attr_map = {
        'pool': 'scrub_volume'
    }

    class Meta:
        fields = '__all__'
        model = models.Scrub
        widgets = {
            'scrub_minute': CronMultiple(
                attrs={'numChoices': 60, 'label': _("minute")},
            ),
            'scrub_hour': CronMultiple(
                attrs={'numChoices': 24, 'label': _("hour")},
            ),
            'scrub_daymonth': CronMultiple(
                attrs={
                    'numChoices': 31,
                    'start': 1,
                    'label': _("day of month")},
            ),
            'scrub_dayweek': forms.CheckboxSelectMultiple(
                choices=choices.WEEKDAYS_CHOICES),
            'scrub_month': forms.CheckboxSelectMultiple(
                choices=choices.MONTHS_CHOICES),
        }

    def __init__(self, *args, **kwargs):
        super(ScrubForm, self).__init__(*args, **kwargs)
        mchoicefield(self, 'scrub_month', [
            1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12
        ])
        mchoicefield(self, 'scrub_dayweek', [
            1, 2, 3, 4, 5, 6, 7
        ])

    def clean_scrub_month(self):
        m = self.data.getlist('scrub_month')
        if len(m) == 12:
            return '*'
        else:
            return ','.join(m)

    def clean_scrub_dayweek(self):
        w = self.data.getlist('scrub_dayweek')
        if len(w) == 7:
            return '*'
        else:
            return ','.join(w)

    def middleware_clean(self, update):
        update['pool'] = update.pop('volume')
        return update


class DiskWipeForm(Form):

    method = forms.ChoiceField(
        label=_("Method"),
        choices=(
            ("QUICK", _("Quick")),
            ("FULL", _("Full with zeros")),
            ("FULL_RANDOM", _("Full with random data")),
        ),
        widget=forms.widgets.RadioSelect(),
    )

    def __init__(self, *args, **kwargs):
        self.disk = kwargs.pop('disk')
        super().__init__(*args, **kwargs)

    def clean(self):
        with client as c:
            if self.disk in c.call('disk.get_reserved'):
                self._errors['__all__'] = self.error_class([
                    _('The disk %s is currently in use and cannot be wiped.') % self.disk
                ])
        return self.cleaned_data


class CreatePassphraseForm(Form):

    passphrase = forms.CharField(
        label=_("Passphrase"),
        widget=forms.widgets.PasswordInput(),
    )
    passphrase2 = forms.CharField(
        label=_("Confirm Passphrase"),
        widget=forms.widgets.PasswordInput(),
    )

    def clean_passphrase2(self):
        pass1 = self.cleaned_data.get("passphrase")
        pass2 = self.cleaned_data.get("passphrase2")
        if pass1 != pass2:
            raise forms.ValidationError(
                _("The passphrases do not match")
            )
        return pass2

    def done(self, volume):
        passphrase = self.cleaned_data.get("passphrase")
        if passphrase is not None:
            passfile = tempfile.mktemp(dir='/tmp/')
            with open(passfile, 'w') as f:
                os.chmod(passfile, 600)
                f.write(passphrase)
        else:
            passfile = None
        notifier().geli_passphrase(volume, passfile, rmrecovery=True)
        if passfile is not None:
            os.unlink(passfile)
        volume.vol_encrypt = 2
        volume.save()


class ChangePassphraseForm(Form):

    adminpw = forms.CharField(
        label=_("Admin password"),
        widget=forms.widgets.PasswordInput(),
    )
    passphrase = forms.CharField(
        label=_("New Passphrase"),
        widget=forms.widgets.PasswordInput(),
    )
    passphrase2 = forms.CharField(
        label=_("Confirm New Passphrase"),
        widget=forms.widgets.PasswordInput(),
    )
    remove = forms.BooleanField(
        label=_("Remove passphrase"),
        required=False,
    )

    def __init__(self, *args, **kwargs):
        super(ChangePassphraseForm, self).__init__(*args, **kwargs)
        self.fields['remove'].widget.attrs['onClick'] = (
            'toggleGeneric("id_remove", ["id_passphrase", '
            '"id_passphrase2"], false);')
        if self.data.get("remove", False):
            self.fields['passphrase'].widget.attrs['disabled'] = 'disabled'
            self.fields['passphrase2'].widget.attrs['disabled'] = 'disabled'

    def clean_adminpw(self):
        pw = self.cleaned_data.get("adminpw")
        valid = False
        for user in bsdUsers.objects.filter(bsdusr_uid=0):
            if user.check_password(pw):
                valid = True
                break
        if valid is False:
            raise forms.ValidationError(
                _("Invalid password")
            )
        return pw

    def clean_passphrase2(self):
        pass1 = self.cleaned_data.get("passphrase")
        pass2 = self.cleaned_data.get("passphrase2")
        if pass1 != pass2:
            raise forms.ValidationError(
                _("The passphrases do not match")
            )
        return pass2

    def clean(self):
        cdata = self.cleaned_data
        if cdata.get("remove"):
            del self._errors['passphrase']
            del self._errors['passphrase2']
        return cdata

    def done(self, volume):
        if self.cleaned_data.get("remove"):
            passphrase = None
        else:
            passphrase = self.cleaned_data.get("passphrase")

        if passphrase is not None:
            passfile = tempfile.mktemp(dir='/tmp/')
            with open(passfile, 'w') as f:
                os.chmod(passfile, 600)
                f.write(passphrase)
        else:
            passfile = None
        notifier().geli_passphrase(volume, passfile)
        if passfile is not None:
            os.unlink(passfile)
            volume.vol_encrypt = 2
        else:
            volume.vol_encrypt = 1
        volume.save()


class UnlockPassphraseForm(Form):

    passphrase = forms.CharField(
        label=_("Passphrase"),
        widget=forms.widgets.PasswordInput(),
        required=False,
    )
    key = FileField(
        label=_("Recovery Key"),
        required=False,
    )
    services = forms.MultipleChoiceField(
        label=_("Restart services"),
        widget=forms.widgets.CheckboxSelectMultiple(),
        initial=['afp', 'cifs', 'ftp', 'iscsitarget', 'jails', 'nfs', 'webdav'],
        required=False,
    )

    def __init__(self, *args, **kwargs):
        super(UnlockPassphraseForm, self).__init__(*args, **kwargs)
        app = appPool.get_app('plugins')
        choices = [
            ('afp', _('AFP')),
            ('cifs', _('CIFS')),
            ('ftp', _('FTP')),
            ('iscsitarget', _('iSCSI')),
            ('nfs', _('NFS')),
            ('webdav', _('WebDAV')),
        ]
        if getattr(app, 'unlock_restart', False):
            choices.append(
                ('jails', _('Jails/Plugins')),
            )
        self.fields['services'].choices = choices

    def clean(self):
        passphrase = self.cleaned_data.get("passphrase")
        key = self.cleaned_data.get("key")
        if not passphrase and key is None:
            self._errors['__all__'] = self.error_class([
                _("You need either a passphrase or a recovery key to unlock")
            ])
        return self.cleaned_data

    def done(self, volume):
        passphrase = self.cleaned_data.get("passphrase")
        key = self.cleaned_data.get("key")
        if passphrase:
            passfile = tempfile.mktemp(dir='/tmp/')
            with open(passfile, 'w') as f:
                os.chmod(passfile, 600)
                f.write(passphrase)
            failed = notifier().geli_attach(volume, passphrase=passfile)
            os.unlink(passfile)
        elif key is not None:
            keyfile = tempfile.mktemp(dir='/tmp/')
            with open(keyfile, 'wb') as f:
                os.chmod(keyfile, 600)
                f.write(key.read())
            failed = notifier().geli_attach(
                volume,
                passphrase=None,
                key=keyfile)
            os.unlink(keyfile)
        else:
            raise ValueError("Need a passphrase or recovery key")
        zimport = notifier().zfs_import(volume.vol_name, id=volume.vol_guid, first_time=False)
        if not zimport:
            if failed > 0:
                msg = _(
                    "Volume could not be imported: %d devices failed to "
                    "decrypt"
                ) % failed
            else:
                msg = _("Volume could not be imported")
            raise MiddlewareError(msg)
        notifier().sync_encrypted(volume=volume)

        _notifier = notifier()
        for svc in self.cleaned_data.get("services"):
            if svc == 'jails':
                with client as c:
                    c.call('core.bulk', 'service.restart', [['jails']])
            else:
                _notifier.restart(svc)
        _notifier.start("ix-warden")
        _notifier.restart("system_datasets")
        _notifier.reload("disk")
        if not _notifier.is_freenas() and _notifier.failover_licensed():
            from freenasUI.failover.enc_helper import LocalEscrowCtl
            escrowctl = LocalEscrowCtl()
            escrowctl.setkey(passphrase)
            try:
                with client as c:
                    c.call('failover.call_remote', 'failover.encryption_setkey', [passphrase])
            except Exception:
                log.warn('Failed to set key on standby node, is it down?', exc_info=True)
            if _notifier.failover_status() != 'MASTER':
                _notifier.failover_force_master()


class KeyForm(Form):

    adminpw = forms.CharField(
        label=_("Root password"),
        widget=forms.widgets.PasswordInput(),
    )

    def __init__(self, *args, **kwargs):
        super(KeyForm, self).__init__(*args, **kwargs)

        if self._api is True:
            del self.fields['adminpw']

    def clean_adminpw(self):
        pw = self.cleaned_data.get("adminpw")
        valid = False
        for user in bsdUsers.objects.filter(bsdusr_uid=0):
            if user.check_password(pw):
                valid = True
                break
        if valid is False:
            raise forms.ValidationError(
                _("Invalid password")
            )
        return pw


class ReKeyForm(KeyForm):

    def __init__(self, *args, **kwargs):
        self.volume = kwargs.pop('volume')
        super(ReKeyForm, self).__init__(*args, **kwargs)

    def done(self):
        notifier().geli_rekey(self.volume)


class VMWarePluginForm(ModelForm):

    oid = forms.CharField(
        widget=forms.widgets.HiddenInput,
        required=False,
    )

    class Meta:
        fields = '__all__'
        model = models.VMWarePlugin
        widgets = {
            'password': forms.widgets.PasswordInput(),
            'datastore': forms.widgets.ComboBox(),
        }

    def __init__(self, *args, **kwargs):
        super(VMWarePluginForm, self).__init__(*args, **kwargs)
        self.fields['password'].required = False
        self.fields['password'].widget.attrs['onchange'] = (
            "vmwareDatastores('%s', dijit.byId('id_datastore'))" % (
                reverse('storage_vmwareplugin_datastores')
            )
        )
        self.fields['filesystem'] = forms.ChoiceField(
            label=self.fields['filesystem'].label,
        )
        volnames = [o.vol_name for o in models.Volume.objects.all()]
        self.fields['filesystem'].choices = [y for y in list(notifier().list_zfs_fsvols().items()) if y[0].split('/')[0] in volnames]
        if self.instance.id:
            self.fields['oid'].initial = self.instance.id

    def clean_password(self):
        password = self.cleaned_data.get('password')
        if not password:
            if self.instance.id:
                return self.instance.password
            else:
                raise forms.ValidationError(_('This field is required.'))
        return password

    def clean(self):
        cdata = self.cleaned_data
        if (
            cdata.get('hostname') and cdata.get('username') and
            cdata.get('password')
        ):
            try:
                with client as c:
                    ds = c.call('vmware.get_datastores', {
                        'hostname': cdata.get('hostname'),
                        'username': cdata.get('username'),
                        'password': cdata.get('password'),
                    })
                datastores = []
                for i in ds.values():
                    datastores += i.keys()
                if cdata.get('datastore') not in datastores:
                    self._errors['datastore'] = self.error_class([_(
                        'Datastore not found in the server.'
                    )])
            except Exception as e:
                self._errors['__all__'] = self.error_class([_(
                    'Failed to connect: %s'
                ) % e])
        return cdata

    def save(self, *args, **kwargs):
        kwargs['commit'] = False
        obj = super(VMWarePluginForm, self).save(*args, **kwargs)
        obj.set_password(self.cleaned_data.get('password'))
        obj.save()
        return obj
