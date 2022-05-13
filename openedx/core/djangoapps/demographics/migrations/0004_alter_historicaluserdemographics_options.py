# Generated by Django 3.2.13 on 2022-05-13 14:35

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('demographics', '0003_auto_20200827_1949'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='historicaluserdemographics',
            options={'get_latest_by': ('history_date', 'history_id'), 'ordering': ('-history_date', '-history_id'), 'verbose_name': 'historical user demographic', 'verbose_name_plural': 'historical user demographic'},
        ),
    ]
