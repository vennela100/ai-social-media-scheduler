from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0009_scheduledpost_stat_comments_scheduledpost_stat_likes_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="video",
            name="source_size_bytes",
            field=models.PositiveBigIntegerField(default=0),
        ),
    ]
