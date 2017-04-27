import base64
import datetime
import logging
import pickle

from django_celery_beat.models import PeriodicTask, CrontabSchedule

try:
    import simplejson as json
except ImportError:
    import json

from django.db import models
from django.contrib.auth.models import User
from django.core.urlresolvers import reverse
from humfrey.sparql.models import Store
from humfrey.update.utils import evaluate_pipeline

DEFINITION_STATUS_CHOICES = (
    ('idle', 'Idle'),
    ('queued', 'Queued'),
    ('active', 'Active'),
)


class UpdateDefinitionAlreadyQueued(AssertionError):
    pass


class UpdateDefinition(models.Model):
    slug = models.SlugField(primary_key=True)
    title = models.CharField(max_length=80)
    description = models.TextField(blank=True)

    owner = models.ForeignKey(User, related_name='owned_updates')

    cron_schedule = models.TextField(blank=True)
    periodic_task = models.ForeignKey(PeriodicTask, null=True, blank=True)

    status = models.CharField(max_length=10, choices=DEFINITION_STATUS_CHOICES, default='idle')
    last_log = models.ForeignKey('UpdateLog', null=True, blank=True)

    last_queued = models.DateTimeField(null=True, blank=True)
    last_started = models.DateTimeField(null=True, blank=True)
    last_completed = models.DateTimeField(null=True, blank=True)
    
    depends_on = models.ManyToManyField('self', symmetrical=False, blank=True)

    class Meta:
        ordering = ('title',)
        permissions = (
            ("view_updatedefinition", "Can view the update definition"),
            ("execute_updatedefinition", "Can perform an update"),
            ("administer_updatedefinition", "Can administer an update definition"),
            ("viewlogs_updatedefinition", "Can view the logs for an update definition"),
        )

    def queue(self, silent=False, trigger=None, user=None, forced=False):
        if self.status != 'idle':
            if silent:
                return
            raise UpdateDefinitionAlreadyQueued()
        self.status = 'queued'
        self.last_queued = datetime.datetime.now()
    
        update_log = UpdateLog.objects.create(update_definition=self,
                                              user=user,
                                              trigger=trigger or '',
                                              queued=self.last_queued,
                                              forced=forced)
    
        self.last_log = update_log
        self.save()

        from humfrey.update import tasks
        tasks.update.delay(update_log_id=update_log.id, slug=self.slug, trigger=trigger)
        return update_log

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse('update:definition-detail', args=[self.slug])
    
    def __init__(self, *args, **kwargs):
        super(UpdateDefinition, self).__init__(*args, **kwargs)
        self._original_cron_schedule = self.cron_schedule
    
    def save(self, *args, **kwargs):
        if self.cron_schedule != self._original_cron_schedule and self.cron_schedule:
            minute, hour, day_of_week = self.cron_schedule.split()[:3]
            
            if not self.periodic_task:
                periodic_task = PeriodicTask(task='humfrey.update.update',
                                                  kwargs=json.dumps({'slug': self.slug,
                                                                     'trigger': 'crontab'}),
                                                  name='Update definition: {0}'.format(self.slug),
                                                  enabled=True)
                periodic_task.save()
                self.periodic_task = periodic_task
                
            crontab = self.periodic_task.crontab or CrontabSchedule()
            crontab.minute = minute
            crontab.hour = hour
            crontab.day_of_week = day_of_week
            crontab.save()
            
            self.periodic_task.crontab = crontab
            self.periodic_task.save()
            
            super(UpdateDefinition, self).save(*args, **kwargs)
            
        elif self.cron_schedule != self._original_cron_schedule and self.periodic_task:
            periodic_task, self.periodic_task = self.periodic_task, None
            
            super(UpdateDefinition, self).save(*args, **kwargs)
            
            periodic_task.crontab.delete()
            periodic_task.delete()
        else:
            super(UpdateDefinition, self).save(*args, **kwargs)
        
    
    def delete(self, *args, **kwargs):
        super(UpdateDefinition, self).delete(*args, **kwargs)
        if self.periodic_task:
            self.periodic_task.crontab.delete()
            self.periodic_task.delete()


class WithLevels(object):
    levels = {'errors': {'label': 'errors',
                         'icon': 'gnome-icons/32x32/dialog-error.png'},
              'warnings': {'label': 'warnings',
                           'icon': 'gnome-icons/32x32/dialog-warning.png'},
              'success': {'label': 'success',
                          'icon': 'gnome-icons/32x32/emblem-default.png'},
              'inprogress': {'label': 'in progress',
                             'icon': 'gnome-icons/32x32/system-run.png'},
              'waiting': {'label': 'waiting',
                          'icon': 'gnome-icons/32x32/dialog-question.png'}}

    @property
    def level(self):
        level = self.log_level
        if level is None:
            return 'waiting'
        if level >= logging.ERROR:
            return 'errors'
        elif level >= logging.WARNING:
            return 'warnings'
        elif level >= 0:
            return 'success'
        else:
            return 'inprogress'

    def get_level_display(self):
        return self.levels[self.level]['label']

    def get_level_icon(self):
        return self.levels[self.level]['icon']


class UpdateLog(models.Model, WithLevels):
    update_definition = models.ForeignKey(UpdateDefinition, related_name="update_log")
    user = models.ForeignKey(User, related_name='update_log', blank=True, null=True)
    forced = models.BooleanField(default=False)

    trigger = models.CharField(max_length=80, blank=True)
    log_level = models.SmallIntegerField(null=True, blank=True)
    log = models.TextField()

    queued = models.DateTimeField(null=True, blank=True)
    started = models.DateTimeField(null=True, blank=True)
    completed = models.DateTimeField(null=True, blank=True)

    def get_absolute_url(self):
        return reverse('update:log-detail', args=[self.update_definition.slug, self.id])

    @property
    def records(self):
        return self.updatelogrecord_set.all().order_by('when')


    def __str__(self):
        return '%s at %s' % (self.update_definition, self.queued)


class UpdatePipeline(models.Model):
    update_definition = models.ForeignKey(UpdateDefinition, related_name="pipelines")
    value = models.TextField()
    stores = models.ManyToManyField(Store)

    def save(self, *args, **kwargs):
        try:
            evaluate_pipeline(self.value)
        except (SyntaxError, NameError) as e:
            raise ValueError(e)
        return super(UpdatePipeline, self).save(*args, **kwargs)


class UpdateVariable(models.Model):
    update_definition = models.ForeignKey(UpdateDefinition, related_name="variables")
    name = models.TextField()
    value = models.TextField()


class Credential(models.Model):
    user = models.ForeignKey(User)
    url = models.CharField(max_length=4096, verbose_name="Base URL")
    username = models.CharField(max_length=128)
    password = models.CharField(max_length=4096)
