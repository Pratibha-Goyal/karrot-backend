# -*- coding: utf-8 -*-
# Generated by Django 1.11.2 on 2017-06-28 22:26
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('groups', '0011_auto_20170223_1629'),
    ]

    operations = [
        migrations.AddField(
            model_name='group',
            name='slack_webhook',
            field=models.CharField(blank=True, max_length=255),
        ),
    ]
