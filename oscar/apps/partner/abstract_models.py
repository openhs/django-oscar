from decimal import Decimal as D

from django.db import models
from django.conf import settings
from django.db.models import get_model
from django.utils.translation import ugettext_lazy as _
from django.utils.importlib import import_module as django_import_module

from oscar.core.utils import slugify
from oscar.core.loading import get_class
from oscar.apps.partner.exceptions import InvalidStockAdjustment
DefaultWrapper = get_class('partner.wrappers', 'DefaultWrapper')


# Cache dict of partner_id => availability wrapper instance
partner_wrappers = None

default_wrapper = DefaultWrapper()


def get_partner_wrapper(partner_id):
    """
    Returns the appropriate partner wrapper given the partner's PK
    """
    if partner_wrappers is None:
        _load_partner_wrappers()
    return partner_wrappers.get(partner_id, default_wrapper)


def _load_partner_wrappers():
    # Prime cache of partner wrapper dict
    global partner_wrappers
    partner_wrappers = {}
    Partner = get_model('partner', 'Partner')
    for code, class_str in settings.OSCAR_PARTNER_WRAPPERS.items():
        try:
            partner = Partner.objects.get(code=code)
        except Partner.DoesNotExist:
            continue
        else:
            module_path, klass = class_str.rsplit('.', 1)
            module = django_import_module(module_path)
            partner_wrappers[partner.id] = getattr(module, klass)()


class AbstractPartner(models.Model):
    """
    Fulfillment partner
    """
    code = models.SlugField(_("Code"), max_length=128, unique=True)
    name = models.CharField(_("Name"), max_length=128, null=True, blank=True)

    # A partner can have users assigned to it.  These can be used
    # to provide authentication for webservices etc.
    users = models.ManyToManyField(
        'auth.User', related_name="partners", blank=True, null=True,
        verbose_name=_("Users"))

    @property
    def display_name(self):
        if not self.name:
            return self.code
        return self.name

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = slugify(self.name)
        super(AbstractPartner, self).save(*args, **kwargs)

    class Meta:
        verbose_name = _('Fulfillment partner')
        verbose_name_plural = _('Fulfillment partners')
        abstract = True
        permissions = (
            ("can_edit_stock_records", _("Can edit stock records")),
            ("can_view_stock_records", _("Can view stock records")),
            ("can_edit_product_range", _("Can edit product range")),
            ("can_view_product_range", _("Can view product range")),
            ("can_edit_order_lines", _("Can edit order lines")),
            ("can_view_order_lines", _("Can view order lines"))
        )

    def __unicode__(self):
        return self.name


class AbstractStockRecord(models.Model):
    """
    A basic stock record.

    This links a product to a partner, together with price and availability
    information.  Most projects will need to subclass this object to add custom
    fields such as lead_time, report_code, min_quantity.

    We deliberately don't store tax information to allow each project
    to subclass this model and put its own fields for convey tax.
    """
    product = models.OneToOneField(
        'catalogue.Product', related_name="stockrecord",
        verbose_name=_("Product"))
    partner = models.ForeignKey('partner.Partner', verbose_name=_("Partner"))

    # The fulfilment partner will often have their own SKU for a product, which
    # we store here.  This will sometimes be the same the product's UPC but not
    # always.  It should be unique per partner.
    partner_sku = models.CharField(_("Partner SKU"), max_length=128)

    # Price info:
    price_currency = models.CharField(
        _("Currency"), max_length=12, default=settings.OSCAR_DEFAULT_CURRENCY)

    # This is the base price for calculations - tax should be applied by the
    # appropriate method.  We don't store tax here as its calculation is highly
    # domain-specific.  It is NULLable because some items don't have a fixed
    # price but require a runtime calculation (possible from an external
    # service).
    price_excl_tax = models.DecimalField(
        _("Price (excl. tax)"), decimal_places=2, max_digits=12,
        blank=True, null=True)

    # Retail price for this item.  This is simply the recommended price from
    # the manufacturer.  If this is used, it is for display purposes only.
    # This prices is the NOT the price charged to the customer.
    price_retail = models.DecimalField(
        _("Price (retail)"), decimal_places=2, max_digits=12,
        blank=True, null=True)

    # Cost price is the price charged by the fulfilment partner.  It is not
    # used (by default) in any price calculations but is often used in
    # reporting so merchants can report on their profit margin.
    cost_price = models.DecimalField(
        _("Cost Price"), decimal_places=2, max_digits=12,
        blank=True, null=True)

    # Number of items in stock
    num_in_stock = models.PositiveIntegerField(
        _("Number in stock"), blank=True, null=True)

    # Threshold for low-stock alerts.  When stock goes beneath this threshold,
    # an alert is triggered so warehouse managers can order more.
    low_stock_threshold = models.PositiveIntegerField(
        _("Low Stock Threshold"), blank=True, null=True)

    # The amount of stock allocated to orders but not fed back to the master
    # stock system.  A typical stock update process will set the num_in_stock
    # variable to a new value and reset num_allocated to zero
    num_allocated = models.IntegerField(
        _("Number allocated"), default=0, blank=True, null=True)

    # Date information
    date_created = models.DateTimeField(_("Date created"), auto_now_add=True)
    date_updated = models.DateTimeField(_("Date updated"), auto_now=True,
                                        db_index=True)

    class Meta:
        abstract = True
        unique_together = ('partner', 'partner_sku')
        verbose_name = _("Stock Record")
        verbose_name_plural = _("Stock Records")

    # 2-stage stock management model

    def allocate(self, quantity):
        """
        Record a stock allocation.

        This normally happens when a product is bought at checkout.  When the
        product is actually shipped, then we 'consume' the allocation.
        """
        if self.num_allocated is None:
            self.num_allocated = 0
        self.num_allocated += quantity
        self.save()
    allocate.alters_data = True

    def is_allocation_consumption_possible(self, quantity):
        return quantity <= min(self.num_allocated, self.num_in_stock)

    def consume_allocation(self, quantity):
        """
        Consume a previous allocation

        This is used when an item is shipped.  We remove the original allocation
        and adjust the number in stock accordingly
        """
        if not self.is_allocation_consumption_possible(quantity):
            raise InvalidStockAdjustment(_('Invalid stock consumption request'))
        self.num_allocated -= quantity
        self.num_in_stock -= quantity
        self.save()
    consume_allocation.alters_data = True

    def cancel_allocation(self, quantity):
        # We ignore requests that request a cancellation of more than the amount already
        # allocated.
        self.num_allocated -= min(self.num_allocated, quantity)
        self.save()
    cancel_allocation.alters_data = True

    @property
    def net_stock_level(self):
        """
        Return the effective number in stock.  This is correct property to show
        the customer, not the num_in_stock field as that doesn't account for
        allocations.  This can be negative in some unusual circumstances
        """
        if self.num_in_stock is None:
            return 0
        if self.num_allocated is None:
            return self.num_in_stock
        return self.num_in_stock - self.num_allocated

    def set_discount_price(self, price):
        """
        A setter method for setting a new price.

        This is called from within the "discount" app, which is responsible
        for applying fixed-discount offers to products.  We use a setter method
        so that this behaviour can be customised in projects.
        """
        self.price_excl_tax = price
        self.save()
    set_discount_price.alters_data = True

    # Price retrieval methods - these default to no tax being applicable
    # These are intended to be overridden.

    @property
    def is_available_to_buy(self):
        """
        Return whether this stockrecord allows the product to be purchased
        """
        return get_partner_wrapper(self.partner_id).is_available_to_buy(self)

    def is_purchase_permitted(self, user=None, quantity=1):
        """
        Return whether this stockrecord allows the product to be purchased by a
        specific user and quantity
        """
        return get_partner_wrapper(self.partner_id).is_purchase_permitted(self, user, quantity)

    @property
    def is_below_threshold(self):
        if self.low_stock_threshold is None:
            return False
        return self.net_stock_level < self.low_stock_threshold

    @property
    def availability_code(self):
        """
        Return an product's availability as a code for use in CSS to add icons
        to the overall availability mark-up.  For example, "instock",
        "unavailable".
        """
        return get_partner_wrapper(self.partner_id).availability_code(self)

    @property
    def availability(self):
        """
        Return a product's availability as a string that can be displayed to the
        user.  For example, "In stock", "Unavailable".
        """
        return get_partner_wrapper(self.partner_id).availability(self)

    def max_purchase_quantity(self, user=None):
        """
        Return an item's availability as a string

        :param user: (optional) The user who wants to purchase
        """
        return get_partner_wrapper(self.partner_id).max_purchase_quantity(self, user)

    @property
    def dispatch_date(self):
        """
        Return the estimated dispatch date for a line
        """
        return get_partner_wrapper(self.partner_id).dispatch_date(self)

    @property
    def lead_time(self):
        return get_partner_wrapper(self.partner_id).lead_time(self)

    @property
    def price_incl_tax(self):
        """
        Return a product's price including tax.

        This defaults to the price_excl_tax as tax calculations are
        domain specific.  This class needs to be subclassed and tax logic
        added to this method.
        """
        if self.price_excl_tax is None:
            return D('0.00')
        return self.price_excl_tax + self.price_tax

    @property
    def price_tax(self):
        """
        Return a product's tax value
        """
        return get_partner_wrapper(self.partner_id).calculate_tax(self)

    def __unicode__(self):
        if self.partner_sku:
            return "%s (%s): %s" % (self.partner.display_name,
                                    self.partner_sku, self.product.title)
        else:
            return "%s: %s" % (self.partner.display_name, self.product.title)


class AbstractStockAlert(models.Model):
    stockrecord = models.ForeignKey(
        'partner.StockRecord', related_name='alerts',
        verbose_name=_("Stock Record"))
    threshold = models.PositiveIntegerField(_("Threshold"))
    OPEN, CLOSED = "Open", "Closed"
    status_choices = (
        (OPEN, _("Open")),
        (CLOSED, _("Closed")),
    )
    status = models.CharField(_("Status"), max_length=128, default=OPEN,
                              choices=status_choices)
    date_created = models.DateTimeField(_("Date Created"), auto_now_add=True)
    date_closed = models.DateTimeField(_("Date Closed"), blank=True, null=True)

    def close(self):
        self.status = self.CLOSED
        self.save()
    close.alters_data = True

    def __unicode__(self):
        return _('<stockalert for "%(stock)s" status %(status)s>') % {'stock': self.stockrecord, 'status': self.status}

    class Meta:
        abstract = True
        ordering = ('-date_created',)
        verbose_name = _('Stock Alert')
        verbose_name_plural = _('Stock Alerts')
