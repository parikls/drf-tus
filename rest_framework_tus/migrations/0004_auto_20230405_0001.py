# Generated by Django 4.0.3 on 2023-04-05 14:14

import collections
from django.db import migrations, models
import jsonfield.fields


class Migration(migrations.Migration):

    dependencies = [
        ('rest_framework_tus', '0003_auto_20170619_0358'),
    ]

    operations = [
        migrations.AlterField(
            model_name='upload',
            name='upload_metadata',
            field=jsonfield.fields.JSONField(load_kwargs={'object_pairs_hook': collections.OrderedDict}),
        ),
    ]