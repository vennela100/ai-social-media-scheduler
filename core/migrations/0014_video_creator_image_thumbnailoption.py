from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0013_video_r2_object_key"),
    ]

    operations = [
        migrations.AddField(
            model_name="video",
            name="creator_image_public_id",
            field=models.CharField(blank=True, default="", max_length=300),
        ),
        migrations.AddField(
            model_name="video",
            name="creator_image_url",
            field=models.URLField(blank=True, default="", max_length=500),
        ),
        migrations.CreateModel(
            name="ThumbnailOption",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("image_url", models.URLField(max_length=500)),
                ("cloudinary_public_id", models.CharField(blank=True, default="", max_length=300)),
                ("title", models.CharField(blank=True, default="", max_length=120)),
                ("prompt", models.TextField(blank=True, default="")),
                ("selected", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "video",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="thumbnail_options",
                        to="core.video",
                    ),
                ),
            ],
            options={
                "ordering": ["created_at"],
            },
        ),
    ]
