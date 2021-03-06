import reversion
from django.db import models
from django.utils.translation import ugettext as _, ugettext_lazy
from modoboa.lib import events
from modoboa.extensions.admin.exceptions import AdminError
from .base import DatesAware
from .domain import Domain


class DomainAlias(DatesAware):
    name = models.CharField(ugettext_lazy("name"), max_length=100, unique=True,
                            help_text=ugettext_lazy("The alias name"))
    target = models.ForeignKey(
        Domain, verbose_name=ugettext_lazy('target'),
        help_text=ugettext_lazy("The domain this alias points to")
    )
    enabled = models.BooleanField(
        ugettext_lazy('enabled'),
        help_text=ugettext_lazy("Check to activate this alias")
    )

    class Meta:
        permissions = (
            ("view_domaliases", "View domain aliases"),
        )
        app_label = 'admin'

    def __unicode__(self):
        return self.name

    def post_create(self, creator):
        from modoboa.lib.permissions import grant_access_to_object
        grant_access_to_object(creator, self, is_owner=True)
        events.raiseEvent("DomainAliasCreated", creator, self)

    def save(self, *args, **kwargs):
        if "creator" in kwargs:
            creator = kwargs["creator"]
            del kwargs["creator"]
        else:
            creator = None
        super(DomainAlias, self).save(*args, **kwargs)
        if creator is not None:
            self.post_create(creator)

    def delete(self):
        from modoboa.lib.permissions import ungrant_access_to_object
        events.raiseEvent("DomainAliasDeleted", self)
        ungrant_access_to_object(self)
        super(DomainAlias, self).delete()

    def from_csv(self, user, row):
        """Create a domain alias from a CSV row

        Expected format: ["domainalias", domain alias name, targeted domain, enabled]

        :param user: a ``User`` object
        :param row: a list containing the alias definition
        """
        if len(row) < 4:
            raise AdminError(_("Invalid line"))
        self.name = row[1].strip()
        domname = row[2].strip()
        try:
            self.target = Domain.objects.get(name=domname)
        except Domain.DoesNotExist:
            raise AdminError(_("Unknown domain %s" % domname))
        self.enabled = row[3].strip() == 'True'
        self.save(creator=user)

    def to_csv(self, csvwriter):
        """Export a domain alias using CSV format

        :param csvwriter: a ``csv.writer`` object
        """
        csvwriter.writerow(["domainalias", self.name,
                            self.target.name, self.enabled])

reversion.register(DomainAlias)
