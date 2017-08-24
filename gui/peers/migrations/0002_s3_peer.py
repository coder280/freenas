# -*- coding: utf-8 -*-
# Generated by Django 1.10.7 on 2017-08-24 13:42
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('peers', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='S3_Peer',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=150, verbose_name='Peer name')),
                ('description', models.CharField(max_length=250, verbose_name='Peer description')),
                ('s3_access_key', models.CharField(help_text='S3 username', max_length=128, verbose_name='Access key of 5 to 20 characters in length')),
                ('s3_secret_key', models.CharField(help_text='S3 password', max_length=128, verbose_name='Secret key of 8 to 40 characters in length')),
            ],
            options={
                'abstract': False,
            },
        ),
    ]
