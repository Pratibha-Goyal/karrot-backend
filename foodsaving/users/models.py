from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import transaction, models
from django.utils import timezone
from django.db.models import EmailField, BooleanField, TextField, CharField, DateTimeField, ForeignKey, Q
from versatileimagefield.fields import VersatileImageField

from foodsaving.base.base_models import BaseModel, LocationModel
from foodsaving.userauth.models import VerificationCode
from foodsaving.utils.email_utils import prepare_mailverification_email, prepare_email
from foodsaving.groups.models import Group, GroupMembership
from foodsaving.webhooks.models import EmailEvent

MAX_DISPLAY_NAME_LENGTH = 80


class UserManager(BaseUserManager):
    use_in_migrations = True

    @transaction.atomic
    def _create_user(self, email, password, display_name=None, is_active=True, **extra_fields):
        """ Creates and saves a user with the given username, email and password.

        """
        email = self._validate_email(email)
        extra_fields['unverified_email'] = email

        user = self.model(
            email=email,
            is_active=is_active,
            display_name=display_name,
            **extra_fields)
        user.set_password(password)
        user.save()
        user._send_welcome_mail()
        return user

    def filter_by_similar_email(self, email):
        return self.filter(email__iexact=email)

    def unverified_or_ignored(self):
        return self.filter(Q(mail_verified=False) | Q(email__in=EmailEvent.objects.ignored_addresses()))

    def active(self):
        return self.filter(deleted=False, is_active=True)

    def get_by_natural_key(self, email):
        """
        As we don't allow sign-ups with similarly cased email addresses,
        we can allow users to login with case spelling mistakes
        """
        return self.get(email__iexact=email)

    def _validate_email(self, email):
        if email is None:
            raise ValueError('The email field must be set')
        return self.normalize_email(email)

    def create_user(self, email=None, password=None, display_name=None,
                    **extra_fields):
        return self._create_user(email, password, display_name, **extra_fields)

    def create_superuser(self, email, password, **extra_fields):
        user = self._create_user(email, password, email, **extra_fields)
        user.is_superuser = True
        user.is_staff = True
        user.save()
        return user


class User(AbstractBaseUser, BaseModel, LocationModel):
    email = EmailField(unique=True, null=True)
    is_active = BooleanField(default=True)
    is_staff = BooleanField(default=False)
    is_superuser = BooleanField(default=False)
    display_name = CharField(max_length=settings.NAME_MAX_LENGTH)
    description = TextField(blank=True)
    language = CharField(max_length=7, default='en')
    mail_verified = BooleanField(default=False)
    unverified_email = EmailField(null=True)
    mobile_number = CharField(max_length=255, blank=True)

    deleted = BooleanField(default=False)
    deleted_at = DateTimeField(default=None, null=True)
    current_group = ForeignKey('groups.Group', blank=True, null=True, on_delete=models.SET_NULL)

    photo = VersatileImageField(
        'Photo',
        upload_to='user__photos',
        null=True,
    )

    objects = UserManager()

    USERNAME_FIELD = 'email'

    def get_full_name(self):
        return self.display_name

    def get_short_name(self):
        return self.display_name

    def delete_photo(self):
        # Deletes Image Renditions
        self.photo.delete_all_created_images()
        # Deletes Original Image
        self.photo.delete(save=False)

    @transaction.atomic
    def verify_mail(self):
        VerificationCode.objects.filter(user=self, type=VerificationCode.EMAIL_VERIFICATION).delete()
        self.email = self.unverified_email
        self.mail_verified = True
        self.save()

    @transaction.atomic
    def _unverify_mail(self):
        VerificationCode.objects.filter(user=self, type=VerificationCode.EMAIL_VERIFICATION).delete()
        VerificationCode.objects.create(user=self, type=VerificationCode.EMAIL_VERIFICATION)
        self.mail_verified = False
        self.save()

    @transaction.atomic
    def update_email(self, unverified_email):
        self.unverified_email = unverified_email
        self._send_mail_change_notification()
        self.send_mail_verification_code()

    @transaction.atomic
    def restore_email(self):
        VerificationCode.objects.filter(user=self, type=VerificationCode.EMAIL_VERIFICATION).delete()
        self.unverified_email = self.email
        self.mail_verified = True
        self.save()

    def update_language(self, language):
        self.language = language

    def _send_mail_change_notification(self):
        prepare_email('changemail_notice', self, {}).send()

    def _send_welcome_mail(self):
        self._unverify_mail()
        prepare_mailverification_email(
            user=self,
            verification_code=VerificationCode.objects.get(user=self, type=VerificationCode.EMAIL_VERIFICATION)
        ).send()

    @transaction.atomic
    def send_mail_verification_code(self):
        self._unverify_mail()
        verification_code = VerificationCode.objects.get(user=self, type=VerificationCode.EMAIL_VERIFICATION)
        prepare_mailverification_email(self, verification_code.code).send()

    @transaction.atomic
    def send_account_deletion_verification_code(self):
        VerificationCode.objects.filter(user=self, type=VerificationCode.ACCOUNT_DELETE).delete()
        prepare_email('accountdelete_request', self, {
            'url': '{hostname}/#/user/delete?code={code}'.format(
                hostname=settings.HOSTNAME,
                code=VerificationCode.objects.create(user=self, type=VerificationCode.ACCOUNT_DELETE).code
            )
        }).send()

    @transaction.atomic
    def reset_password(self):
        VerificationCode.objects.filter(user=self, type=VerificationCode.PASSWORD_RESET).delete()
        VerificationCode.objects.create(user=self, type=VerificationCode.PASSWORD_RESET)
        prepare_email('passwordreset', self, {
            'url': '{hostname}/#/password/reset?code={code}'.format(
                hostname=settings.HOSTNAME,
                code=VerificationCode.objects.get(user=self, type=VerificationCode.PASSWORD_RESET).code
            )
        }).send()

    @transaction.atomic
    def change_password(self, new_password):
        self.set_password(new_password)
        self.save()
        prepare_email('passwordchange', self, {}).send()
        VerificationCode.objects.filter(user=self, type=VerificationCode.PASSWORD_RESET).delete()

    @transaction.atomic
    def delete_(self):
        """
        Delete the user.

        To keep historic pickup infos, keep the user account but clear personal data.
        """
        # Emits pre_delete and post_delete signals, they are used to remove the user from pick-ups
        for _ in Group.objects.filter(members__in=[self, ]):
            GroupMembership.objects.filter(group=_, user=self).delete()

        self.description = ''
        self.set_unusable_password()
        self.mail = None
        self.is_active = False
        self.is_staff = False
        self.mail_verified = False
        self.unverified_email = None
        self.deleted_at = timezone.now()
        self.deleted = True
        self.delete_photo()
        self.save()

        prepare_email('accountdelete_success', self, {}).send()

    def has_perm(self, perm, obj=None):
        # temporarily only allow access for admins
        return self.is_superuser

    def has_module_perms(self, app_label):
        # temporarily only allow access for admins
        return self.is_superuser
