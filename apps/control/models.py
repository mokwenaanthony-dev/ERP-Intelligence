"""What people decided about the control tower's recommendations, and the AI assistant's switch (Wave 9b).

* ``Decision`` - one owner's accept or reject of one recommendation, with the reason. One per problem
  (``company_code`` + ``key``): repeating the same decision returns the first, a different one is
  refused, so the record cannot be rewritten after the fact (ADR 0010, ADR 0018). The recommendation
  and its expected impact are copied at the moment of deciding, so the record still says what was
  proposed after the problem itself is gone.
* ``AssistantSetting`` - the one switch that lets the AI assistant send facts to an outside model.
  Off by default; only an administrator turns it on, and it also needs a provider key in the
  environment (never in the database).
* ``AssistantAnswer`` - an explanation the model gave, stored by the fingerprint of the facts it was
  given, so asking again about the same facts returns the same answer (and costs nothing).
"""
from django.conf import settings
from django.db import models
from django.utils import timezone


class Decision(models.Model):
    """An owner's decision about one recommendation of the problems inbox."""

    class Outcome(models.TextChoices):
        ACCEPTED = "ACCEPTED", "Accepted"
        REJECTED = "REJECTED", "Rejected"

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    key = models.CharField(max_length=120, help_text="The problem's stable key, e.g. late-order:SO00000002/10.")
    kind = models.CharField(max_length=30, help_text="The kind of problem: the key up to its first colon.")
    area = models.CharField(max_length=20)
    title = models.CharField(max_length=200)
    action = models.TextField(help_text="The recommended action, as it was proposed.")
    kpi = models.CharField(max_length=30, blank=True, default="", help_text="The KPI the action should improve.")
    impact_value = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True,
                                       help_text="Money involved, when there is any (company currency).")
    outcome = models.CharField(max_length=10, choices=Outcome.choices)
    reason = models.TextField(blank=True, default="", help_text="Why; required when rejecting.")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    decided_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-decided_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["company_code", "key"], name="one_decision_per_problem")]
        indexes = [models.Index(fields=["company_code", "kind"])]

    def __str__(self):
        return f"{self.key} {self.outcome}"


class AssistantSetting(models.Model):
    """The AI assistant's opt-in switch: a single row (id 1); missing means off."""

    enabled = models.BooleanField(default=False)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="+")
    changed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"AI assistant {'on' if self.enabled else 'off'}"


class AssistantAnswer(models.Model):
    """One explanation from the model, kept by the fingerprint of its question, facts and model."""

    fingerprint = models.CharField(max_length=64, unique=True)
    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    key = models.CharField(max_length=120)
    model = models.CharField(max_length=60)
    answer = models.TextField()
    cited = models.JSONField(default=list, help_text="The fact ids the answer cites, e.g. [\"F1\", \"F4\"].")
    grounded = models.BooleanField(help_text="False when the answer cites none of the facts it was given.")
    asked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.key} ({self.model})"
