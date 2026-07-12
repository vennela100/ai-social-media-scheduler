from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0014_video_creator_image_thumbnailoption"),
    ]

    operations = [
        migrations.DeleteModel(name="ThumbnailOption"),
        migrations.RemoveField(model_name="video", name="creator_image_public_id"),
        migrations.RemoveField(model_name="video", name="creator_image_url"),
    ]
