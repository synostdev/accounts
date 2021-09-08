# Generated by Django 3.2.6 on 2021-08-30 14:49

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("authentik_flows", "0023_alter_flow_background"),
    ]

    operations = [
        migrations.AlterField(
            model_name="flow",
            name="compatibility_mode",
            field=models.BooleanField(
                default=False,
                help_text="Enable compatibility mode, increases compatibility with password managers on mobile devices.",
            ),
        ),
    ]