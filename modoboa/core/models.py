import sys
import os
import re
import hashlib
import crypt
import base64
from random import Random
import reversion
from django.db import models
from django.dispatch import receiver
from django.utils import timezone
from django.utils.translation import ugettext as _, ugettext_lazy
from django.utils.crypto import constant_time_compare
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes import generic
from django.contrib.auth.models import (
    UserManager, Group, AbstractBaseUser, PermissionsMixin
)
from modoboa.lib import events, md5crypt, parameters
from modoboa.lib.exceptions import PermDeniedException
from modoboa.lib.sysutils import exec_cmd
from modoboa.core.extensions import exts_pool
from modoboa.core.exceptions import AdminError

try:
    from modoboa.lib.ldaputils import *
    ldap_available = True
except ImportError:
    ldap_available = False


class User(AbstractBaseUser, PermissionsMixin):
    """Custom User model.

    It overloads the way passwords are stored into the database. The
    main reason to change this mechanism is to ensure the
    compatibility with the way Dovecot stores passwords.

    It also adds new attributes and methods.
    """
    username = models.CharField(max_length=254, unique=True)
    first_name = models.CharField(max_length=30, blank=True)
    last_name = models.CharField(max_length=30, blank=True)
    email = models.EmailField(max_length=254, blank=True)
    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    date_joined = models.DateTimeField(default=timezone.now)
    is_local = models.BooleanField(default=True)

    objects = UserManager()

    USERNAME_FIELD = 'username'
    REQUIRED_FIELDS = ['email']

    class Meta:
        ordering = ["username"]

    password_expr = re.compile(r'(\{(\w+)\}|(\$1\$))(.+)')

    def delete(self, fromuser, *args, **kwargs):
        """Custom delete method

        To check permissions properly, we need to make a distinction
        between 2 cases:

        * If the user owns a mailbox, the check is made on that object
          (useful for domain admins)

        * Otherwise, the check is made on the user
        """
        from modoboa.lib.permissions import \
            get_object_owner, grant_access_to_object, ungrant_access_to_object

        if fromuser == self:
            raise AdminError(_("You can't delete your own account"))

        if not fromuser.can_access(self):
            raise PermDeniedException

        owner = get_object_owner(self)
        for ooentry in self.objectaccess_set.filter(is_owner=True):
            if ooentry.content_object is not None:
                grant_access_to_object(owner, ooentry.content_object, True)

        events.raiseEvent("AccountDeleted", self, fromuser, **kwargs)
        ungrant_access_to_object(self)
        super(User, self).delete()

    def _crypt_password(self, raw_value):
        scheme = parameters.get_admin("PASSWORD_SCHEME")
        if type(raw_value) is unicode:
            raw_value = raw_value.encode("utf-8")
        if scheme == "crypt":
            salt = "".join(Random().sample(string.letters + string.digits, 2))
            result = crypt.crypt(raw_value, salt)
            prefix = "{CRYPT}"
        elif scheme == "md5":
            obj = hashlib.md5(raw_value)
            result = obj.hexdigest()
            prefix = "{MD5}"
        # The md5crypt scheme is the only supported method that has both:
        # (a) a salt ("crypt" has this too),
        # (b) supports passwords lengths of more than 8 characters (all except
        #     "crypt").
        elif scheme == "md5crypt":
            # The salt may vary from 12 to 48 bits. (Using all six bytes here
            # with a subset of characters means we get only 35 random bits.)
            salt = "".join(Random().sample(string.letters + string.digits, 6))
            result = md5crypt(raw_value, salt)
            prefix = ""  # the result already has $1$ prepended to it
                         # to signify what this is
        elif scheme == "sha256":
            obj = hashlib.sha256(raw_value)
            result = base64.b64encode(obj.digest())
            prefix = "{SHA256}"
        else:
            scheme = "plain"
            result = raw_value
            prefix = "{PLAIN}"
        return "%s%s" % (prefix, result)

    def set_password(self, raw_value, curvalue=None):
        """Password update

        Update the current mailbox's password with the given clear
        value. This value is encrypted according to the defined method
        before it is saved.

        :param raw_value: the new password's value
        :param curvalue: the current password (for LDAP authentication)
        """
        if parameters.get_admin("AUTHENTICATION_TYPE") == "local":
            self.password = self._crypt_password(raw_value)
        else:
            if not ldap_available:
                raise AdminError(
                    _("Failed to update password: LDAP module not installed")
                )

            ab = LDAPAuthBackend()
            try:
                ab.update_user_password(self.username, curvalue, raw_value)
            except LDAPException, e:
                raise AdminError(_("Failed to update password: %s" % str(e)))
        events.raiseEvent(
            "PasswordUpdated", self, raw_value, self.pk is None
        )

    def check_password(self, raw_value):
        m = self.password_expr.match(self.password)
        if m is None:
            return False
        if type(raw_value) is unicode:
            raw_value = raw_value.encode("utf-8")
        scheme = (m.group(2) or m.group(3)).lower()
        val2 = m.group(4)
        if scheme == u"crypt":
            val1 = crypt.crypt(raw_value, val2)
        elif scheme == u"md5":
            val1 = hashlib.md5(raw_value).hexdigest()
        elif scheme == u"sha256":
            val1 = base64.b64encode(hashlib.sha256(raw_value).digest())
        elif scheme == u"$1$":  # md5crypt
            salt, hashed = val2.split('$')
            val1 = md5crypt(raw_value, str(salt))
            val2 = self.password  # re-add scheme for comparison below
        else:
            val1 = raw_value
        return constant_time_compare(val1, val2)

    @property
    def tags(self):
        return [{"name": "account", "label": _("account"), "type": "idt"},
                {"name": self.group, "label": self.group,
                 "type": "grp", "color": "info"}]

    @property
    def fullname(self):
        if self.first_name != u"":
            return u"%s %s" % (self.first_name, self.last_name)
        return self.username

    @property
    def identity(self):
        return self.username

    @property
    def name_or_rcpt(self):
        if self.first_name != "":
            return "%s %s" % (self.first_name, self.last_name)
        return "----"

    @property
    def group(self):
        if self.is_superuser:
            return "SuperAdmins"
        try:
            return self.groups.all()[0].name
        except IndexError:
            return "SimpleUsers"

    @property
    def enabled(self):
        return self.is_active

    @property
    def encoded_address(self):
        from email.header import Header
        if self.first_name != "" or self.last_name != "":
            return "%s <%s>" % \
                (Header(self.fullname, 'utf8').encode(), self.email)
        return self.email

    def belongs_to_group(self, name):
        """Simple shortcut to check if this user is a member of a
        specific group.

        :param name: the group's name
        :return: a boolean
        """
        try:
            self.groups.get(name=name)
        except Group.DoesNotExist:
            return False
        return True

    def is_owner(self, obj):
        """Tell is the user is the unique owner of this object

        :param obj: an object inheriting from ``models.Model``
        :return: a boolean
        """
        ct = ContentType.objects.get_for_model(obj)
        try:
            ooentry = self.objectaccess_set.get(content_type=ct, object_id=obj.id)
        except ObjectAccess.DoesNotExist:
            return False
        return ooentry.is_owner

    def can_access(self, obj):
        """Check if the user can access a specific object

        This function is recursive : if the given user hasn't got direct
        access to this object and if he has got access other ``User``
        objects, we check if one of those users owns the object.

        :param obj: a admin object
        :return: a boolean
        """
        if self.is_superuser:
            return True

        ct = ContentType.objects.get_for_model(obj)
        try:
            ooentry = self.objectaccess_set.get(content_type=ct, object_id=obj.id)
        except ObjectAccess.DoesNotExist:
            pass
        else:
            return True
        if ct.model == "user":
            return False

        ct = ContentType.objects.get_for_model(self)
        qs = self.objectaccess_set.filter(content_type=ct)
        for ooentry in qs.all():
            if ooentry.content_object.is_owner(obj):
                return True
        return False

    def set_role(self, role):
        """Set administrative role for this account

        :param string role: the role to set
        """
        if role is None or self.group == role:
            return
        events.raiseEvent("RoleChanged", self, role)
        self.groups.clear()
        if role == "SuperAdmins":
            self.is_superuser = True
        else:
            if self.is_superuser:
                ObjectAccess.objects.filter(user=self).delete()
            self.is_superuser = False
            try:
                self.groups.add(Group.objects.get(name=role))
            except Group.DoesNotExist:
                self.groups.add(Group.objects.get(name="SimpleUsers"))
            if self.group != "SimpleUsers" and not self.can_access(self):
                from modoboa.lib.permissions import grant_access_to_object
                grant_access_to_object(self, self)
        self.save()

    def post_create(self, creator):
        from modoboa.lib.permissions import grant_access_to_object
        grant_access_to_object(creator, self, is_owner=True)
        events.raiseEvent("AccountCreated", self)

    def save(self, *args, **kwargs):
        if "creator" in kwargs:
            creator = kwargs["creator"]
            del kwargs["creator"]
        else:
            creator = None
        super(User, self).save(*args, **kwargs)
        if creator is not None:
            self.post_create(creator)

    def from_csv(self, user, row, crypt_password=True):
        """Create a new account from a CSV file entry

        The expected order is the following::

        "account", loginname, password, first name, last name, enabled, group, address[, domain, ...]

        :param user: a ``core.User`` instance
        :param row: a list containing the expected information
        :param crypt_password:
        """
        if len(row) < 7:
            raise AdminError(_("Invalid line"))
        role = row[6].strip()
        if not user.is_superuser and not role in ["SimpleUsers", "DomainAdmins"]:
            raise PermDeniedException(
                _("You can't import an account with a role greater than yours")
            )
        self.username = row[1].strip()
        if role == "SimpleUsers":
            if (len(row) < 8 or not row[7].strip()):
                raise AdminError(
                    _("The simple user '%s' must have a valid email address" % self.username)
                )
            if self.username != row[7].strip():
                raise AdminError(
                    _("username and email fields must not differ for '%s'" % self.username)
                )

        if crypt_password:
            self.set_password(row[2].strip())
        else:
            self.password = row[2].strip()
        self.first_name = row[3].strip()
        self.last_name = row[4].strip()
        self.is_active = (row[5].strip() == 'True')
        self.save(creator=user)
        self.set_role(role)
        if len(row) < 8:
            return
        events.raiseEvent("AccountImported", user, self, row[7:])

    def to_csv(self, csvwriter):
        row = ["account", self.username.encode("utf-8"), self.password.encode("utf-8"),
               self.first_name.encode("utf-8"), self.last_name.encode("utf-8"),
               self.is_active, self.group, self.email.encode("utf-8")]
        row += events.raiseQueryEvent("AccountExported", self)
        csvwriter.writerow(row)

reversion.register(User)


def populate_callback(user):
    """Populate callback

    If the LDAP authentication backend is in use, this callback will
    be called each time a new user authenticates succesfuly to
    Modoboa. This function is in charge of creating the mailbox
    associated to the provided ``User`` object.

    :param user: a ``User`` instance
    """
    from modoboa.lib.permissions import grant_access_to_object

    sadmins = User.objects.filter(is_superuser=True)
    user.set_role("SimpleUsers")
    user.post_create(sadmins[0])
    for su in sadmins[1:]:
        grant_access_to_object(su, user)
    events.raiseEvent("AccountAutoCreated", user)


class ObjectAccess(models.Model):
    user = models.ForeignKey(User)
    content_type = models.ForeignKey(ContentType)
    object_id = models.PositiveIntegerField()
    content_object = generic.GenericForeignKey('content_type', 'object_id')
    is_owner = models.BooleanField(default=False)

    class Meta:
        unique_together = (("user", "content_type", "object_id"),)

    def __unicode__(self):
        return "%s => %s (%s)" % (self.user, self.content_object, self.content_type)


class Extension(models.Model):
    name = models.CharField(max_length=150)
    enabled = models.BooleanField(
        ugettext_lazy('enabled'),
        help_text=ugettext_lazy("Check to enable this extension")
    )

    def __init__(self, *args, **kwargs):
        super(Extension, self).__init__(*args, **kwargs)

    def __unicode__(self):
        return self.name

    def __get_ext_instance(self):
        if not self.name:
            return None
        if hasattr(self, "instance") and self.instance:
            return
        self.instance = exts_pool.get_extension(self.name)
        if self.instance:
            self.__get_ext_dir()

    def __get_ext_dir(self):
        modname = self.instance.__module__
        path = os.path.realpath(sys.modules[modname].__file__)
        self.extdir = os.path.dirname(path)

    def on(self):
        self.enabled = True
        self.save()

        self.__get_ext_instance()
        self.instance.load()
        self.instance.init()

        if self.instance.needs_media:
            path = os.path.join(settings.MEDIA_ROOT, self.name)
            exec_cmd("mkdir %s" % path)

        events.raiseEvent("ExtEnabled", self)

    def off(self):
        self.__get_ext_instance()
        if self.instance is None:
            return
        self.instance.destroy()

        self.enabled = False
        self.save()

        if self.instance.needs_media:
            path = os.path.join(settings.MEDIA_ROOT, self.name)
            exec_cmd("rm -r %s" % path)

        events.raiseEvent("ExtDisabled", self)

reversion.register(Extension)


class Log(models.Model):
    date_created = models.DateTimeField(auto_now_add=True)
    message = models.CharField(max_length=255)
    level = models.CharField(max_length=15)
    logger = models.CharField(max_length=30)


@receiver(reversion.post_revision_commit)
def post_revision_commit(sender, **kwargs):
    import logging

    if kwargs["revision"].user is None:
        return
    logger = logging.getLogger("modoboa.admin")
    for version in kwargs["versions"]:
        if version.type == reversion.models.VERSION_ADD:
            action = _("added")
            level = "info"
        elif version.type == reversion.models.VERSION_CHANGE:
            action = _("modified")
            level = "warning"
        else:
            action = _("deleted")
            level = "critical"
        message = _("%(object)s '%(name)s' %(action)s by user %(user)s") % {
            "object": unicode(version.content_type).capitalize(),
            "name": version.object_repr, "action": action,
            "user": kwargs["revision"].user.username
        }
        getattr(logger, level)(message)
