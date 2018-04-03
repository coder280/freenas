from middlewared.schema import accepts, Bool, Dict, Int, Patch, Str
from middlewared.service import CRUDService, ValidationErrors
from middlewared.validators import Range


class CronJobService(CRUDService):

    class Config:
        datastore = 'tasks.cronjob'
        datastore_prefix = 'cron_'
        namespace = 'cronjob'

    async def validate_data(self, data, schema):
        verrors = ValidationErrors()

        weekdays = data.get('dayweek')
        if weekdays and weekdays != '*':
            try:
                weekdays = [int(day) for day in weekdays.split(',')]
            except ValueError:
                verrors.add(
                    f'{schema}.weekday',
                    'Please specify valid week days'
                )
            else:
                if len([day for day in weekdays if day not in range(1, 8)]) > 0:
                    verrors.add(
                        f'{schema}.weekday',
                        'The week days should be in range of 1-7 inclusive'
                    )

        months = data.get('month')
        if months and months != '*':
            try:
                months = [int(month) for month in months.split(',')]
            except ValueError:
                verrors.add(
                    f'{schema}.month',
                    'Please specify valid months'
                )
            else:
                if len([month for month in months if month not in range(1, 13)]) > 0:
                    verrors.add(
                        f'{schema}.month',
                        'The months should be in range of 1-12 inclusive'
                    )

        user = data.get('user')
        if user:
            # Windows users can have spaces in their usernames
            # http://www.freebsd.org/cgi/query-pr.cgi?pr=164808
            if ' ' in user:
                verrors.add(
                    f'{schema}.user',
                    'Usernames cannot have spaces'
                )

            elif not (
                await self.middleware.call(
                    'notifier.get_user_object',
                    user
                )
            ):
                verrors.add(
                    f'{schema}.user',
                    'Specified user does not exist'
                )

        return verrors, data

    @accepts(
        Dict(
            'cron_job_create',
            Bool('enabled'),
            Bool('stderr'),
            Bool('stdout'),
            Str('command', required=True),
            Str('daymonth'),
            Str('dayweek'),
            Str('description'),
            Str('hour'),
            Str('minute'),
            Str('month'),
            Str('user', required=True),
            register=True
        )
    )
    async def do_create(self, data):
        verrors, data = await self.validate_data(data, 'cron_job_create')
        if verrors:
            raise verrors

        data['id'] = await self.middleware.call(
            'datastore.insert',
            self._config.datastore,
            data,
            {'prefix': self._config.datastore_prefix}
        )

        await self.middleware.call(
            'service.restart',
            'cron',
            {'onetime': False}
        )

        return data

    @accepts(
        Int('id', validators=[Range(min=1)]),
        Patch('cron_job_create', 'cron_job_update', ('attr', {'update': True}))
    )
    async def do_update(self, id, data):
        task_data = await self.query(filters=[('id', '=', id)], options={'get': True})
        original_data = task_data.copy()
        task_data.update(data)
        verrors, task_data = await self.validate_data(task_data, 'cron_job_update')

        if verrors:
            raise verrors

        if len(set(task_data.items()) ^ set(original_data.items())) > 0:

            await self.middleware.call(
                'datastore.update',
                self._config.datastore,
                id,
                task_data,
                {'prefix': self._config.datastore_prefix}
            )

            await self.middleware.call(
                'service.restart',
                'cron',
                {'onetime': False}
            )

        return await self.query(filters=[('id', '=', id)], options={'get': True})

    @accepts(
        Int('id')
    )
    async def do_delete(self, id):
        response = await self.middleware.call(
            'datastore.delete',
            self._config.datastore,
            id
        )

        await self.middleware.call(
            'service.restart',
            'cron',
            {'onetime': False}
        )
        return response
