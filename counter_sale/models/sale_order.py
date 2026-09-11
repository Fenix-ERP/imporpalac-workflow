from datetime import datetime, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare


class SaleOrder(models.Model):
    _inherit = "sale.order"
    is_inter_company = fields.Boolean(
        compute="_compute_is_inter_company_sale", store=False
    )

    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if "partner_id" in fields_list:
            res["partner_id"] = 7
        return res

    @api.model_create_multi
    def create(self, vals_list):
        """Override create to validate prices and pricelist"""
        order = super().create(vals_list)
        order._validate_prices_and_pricelist()
        return order

    @api.onchange("pricelist_id")
    def _validate_prices_and_pricelist(self):
        for order in self:
            order.order_line.line_pricelist_id = order.pricelist_id
            order.order_line.validate_pricelist()
            order.order_line._recompute_prices()

    @api.depends("order_line")
    def _compute_pending_approval(self):
        res = super()._compute_pending_approval()
        wholesale = self.env.ref(
            "counter_sale.pricelist_wholesale", raise_if_not_found=False
        )
        special = self.env.ref(
            "counter_sale.pricelist_special", raise_if_not_found=False
        )
        card = self.env.ref("counter_sale.pricelist_card", raise_if_not_found=False)
        if not wholesale or not special or not card:
            return res
        for order in self:
            to_approve = order.pending_approval
            lines_to_approval = order.order_line.filtered(
                lambda line: line.line_pricelist_id in [wholesale, special, card]
                and line.discount > 0
                or line.is_lower_than_minimum
            )
            if lines_to_approval:
                to_approve = True
            order.pending_approval = to_approve
        return res

    @api.depends("partner_id")
    def _compute_is_inter_company_sale(self):
        internal_partners = (
            self.env["res.company"].sudo().search([]).mapped("partner_id")
        )
        for order in self:
            order.is_inter_company = order.partner_id in internal_partners

    rectified_order_id = fields.Many2one(
        "sale.order", string="Rectified Sale Order", readonly=True
    )
    expired = fields.Boolean(readonly=False)

    product_pricelist_domain = fields.Char(
        compute="_compute_product_pricelist_domain",
    )

    @api.depends(
        "partner_id",
        "partner_id.property_product_pricelist",
        "company_id.product_pricelist_ids_default",
    )
    def _compute_product_pricelist_domain(self):
        allowed_pricelist = self.company_id.product_pricelist_ids_default
        allowed_ids = allowed_pricelist.ids if allowed_pricelist else []
        for order in self:
            if order.partner_id:
                allowed_ids.append(order.partner_id.property_product_pricelist.id)
            domain = [("id", "in", allowed_ids)]
            order.product_pricelist_domain = str(domain)

    @api.onchange("partner_id", "company_id")
    def _onchange_pricelist_id_force_first(self):
        allowed = self.company_id.product_pricelist_ids_default
        partner_pricelist = self.partner_id.property_product_pricelist
        if partner_pricelist:
            allowed = partner_pricelist + (allowed - partner_pricelist)
        self.pricelist_id = allowed[:1]

    @api.onchange("pricelist_id")
    def _onchange_pricelist_id_show_update_prices(self):
        self._recompute_prices()
        res = super()._onchange_pricelist_id_show_update_prices()
        self.show_update_pricelist = False
        self._validate_prices_and_pricelist()
        return res

    @api.model
    def action_print_ticket(self, order_id):
        order = self.browse(order_id)
        if order.state != "sale":
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Invalid status"),
                    "message": _("The order must be in Confirmed state to print."),
                    "sticky": False,
                    "type": "danger",
                },
            }
        return {
            "type": "ir.actions.client",
            "tag": "print_sale_order",
            "params": {
                "order_id": order.id,
            },
        }

    def action_confirm(self):
        self = self.sudo()
        self.ensure_one()
        res = super(SaleOrder, self).action_confirm()
        if self.state == "draft":
            return res
        self.add_or_update_lines_info(
            [
                {"name": _("Order"), "description": self.name},
                {
                    "name": _("Payment Method"),
                    "description": self.payment_method.name,
                },
            ]
        )
        rectified_related_pickings = self.rectified_order_id.picking_ids
        related_pickings = self.picking_ids
        for old_picking, new_picking in zip(
            rectified_related_pickings, related_pickings
        ):
            new_picking.is_rectified = True
            for old_move in old_picking.move_ids:
                new_move = new_picking.move_ids.filtered(
                    lambda m: m.product_id == old_move.product_id
                )
                if new_move:
                    new_move.rectified_product_uom_qty = (
                        old_move.rectified_product_uom_qty
                    )
                    new_move.rectified_quantity = old_move.rectified_quantity
        journal_payment_ids = self.warehouse_id.payment_method_ids.filtered(
            lambda line: line.payment_type_id.id == self.payment_method.id
        ).journal_payment_ids
        journal_payment_id = journal_payment_ids[0] if journal_payment_ids else False
        if not journal_payment_id and self.payment_method.code != "credit":
            raise ValidationError(
                _(
                    "There is no payment journal for %(payment_method)s "
                    "in warehouse %(warehouse)s, please assign it."
                )
                % {
                    "payment_method": self.payment_method.display_name,
                    "warehouse": self.warehouse_id.display_name,
                }
            )
        if self.payment_method.code != "credit":
            rectified_amount = 0
            if self.rectified_order_id:
                sale_order_payment = (
                    self.env["sale.order.payment"]
                    .sudo()
                    .search([("order_id", "=", self.rectified_order_id.id)])
                )
                if sale_order_payment.state == "cancel":
                    rectified_amount = 0
                else:
                    rectified_amount = self.rectified_order_id.amount_total
            journal_payment_domain = (
                "[('id', 'in', %s)]" % journal_payment_ids.ids or []
            )
            self.sudo().env["sale.order.payment"].create(
                {
                    "client_id": self.partner_id.id,
                    "order_id": self.id,
                    "date_order": self.date_order,
                    "amount": self.amount_total,
                    "rectified_order_id": self.rectified_order_id.id,
                    "rectified_date_order": self.rectified_order_id.date_order,
                    "rectified_amount": rectified_amount,
                    "journal_id": journal_payment_id.id,
                    "journal_domain": journal_payment_domain,
                    "payment_method": self.payment_method.id,
                    "state": "draft",
                    "company_id": self.company_id.id,
                    "pf_branch_id": self.pf_branch_id.id,
                }
            )
        else:
            related_pickings.sudo().write({"payment_state": "credit"})
        return res

    def action_cancel(self):
        self.ensure_one()
        active_invoices = self.invoice_ids.filtered(lambda inv: inv.state == "posted")
        if active_invoices:
            raise UserError(
                _("This order already has an invoice assigned and cannot be cancelled.")
            )
        dissable_locked = self.env.context.get("force_dissable_locked", False)
        if dissable_locked:
            self.sudo().locked = False
        res = super(SaleOrder, self).action_cancel()
        if dissable_locked:
            self.sudo().locked = True
        return res

    def action_expire_quotations(self, hours=48):
        expiration_time = datetime.now() - timedelta(hours=hours)
        expired_orders = self.sudo().search(
            [
                ("state", "=", "draft"),
                ("create_date", "<=", expiration_time),
            ]
        )
        expired_orders.sudo().write({"state": "cancel", "expired": True})


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    product_pricelist_domain = fields.Char(
        related="order_id.product_pricelist_domain",
    )
    line_pricelist_id = fields.Many2one("product.pricelist")

    is_special_pricelist = fields.Boolean(compute="_compute_is_special_pricelist")
    is_lower_than_minimum = fields.Boolean(compute="_compute_is_lower_than_minimum")

    @api.depends("line_pricelist_id")
    def _compute_is_special_pricelist(self):
        refs = [
            "counter_sale.pricelist_wholesale",
            "counter_sale.pricelist_special",
            "counter_sale.pricelist_card",
        ]
        pricelist_ids = [
            self.env.ref(r).id
            for r in refs
            if self.env.ref(r, raise_if_not_found=False)
        ]
        for rec in self:
            rec.is_special_pricelist = rec.line_pricelist_id.id in pricelist_ids

    def _check_price_unit_amount(self):
        for line in self:
            if not line.product_id or line.is_special_pricelist:
                continue

            cost = line.product_id.standard_price
            taxes = line.tax_id.compute_all(
                cost,
                currency=line.currency_id,
                quantity=1.0,
                product=line.product_id,
                partner=line.order_id.partner_id if line.order_id else None,
            )

            total_cost_with_tax = taxes["total_included"]
            if line.price_unit < total_cost_with_tax:
                raise ValidationError(
                    _(
                        "The unit price cannot be less than the cost price"
                        " with taxes for product %s."
                    )
                    % line.product_id.display_name
                )

    @api.onchange("product_id")
    def _onchange_product_id_set_pricelist(self):
        for line in self:
            if not line.line_pricelist_id and line.product_id and line.order_id:
                line.line_pricelist_id = line.order_id.pricelist_id

    @api.onchange("line_pricelist_id")
    def _onchange_line_pricelist_id(self):
        self.validate_pricelist()
        self._recompute_prices()

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        for line in lines:
            if line.line_pricelist_id:
                continue
            line.line_pricelist_id = line.order_id.pricelist_id
            line.validate_pricelist()
            line._recompute_prices()
        return lines

    def write(self, vals):
        res = super().write(vals)
        if set(vals.keys()) == {"line_pricelist_id"}:
            self._recompute_prices()
        return res

    @api.depends("price_unit")
    def _compute_is_lower_than_minimum(self):
        wholesale = self.env.ref(
            "counter_sale.pricelist_wholesale", raise_if_not_found=False
        )
        precision = self.env["decimal.precision"].precision_get("Product Price")
        if not wholesale:
            return
        for line in self:
            product = line.product_id
            if product.type != "product":
                continue
            qty = line.product_uom_qty or 1
            min_price = wholesale._get_product_price(product, qty)
            discount = line.discount or 0.0
            current_price = line.price_unit
            current_price = current_price * (1 - discount / 100.0)
            if float_compare(current_price, min_price, precision_digits=precision) < 0:
                line.is_lower_than_minimum = True
            else:
                line.is_lower_than_minimum = False

    def validate_pricelist(self):
        wholesale = self.env.ref(
            "counter_sale.pricelist_wholesale", raise_if_not_found=False
        )
        special = self.env.ref(
            "counter_sale.pricelist_special", raise_if_not_found=False
        )
        card = self.env.ref("counter_sale.pricelist_card", raise_if_not_found=False)
        if not wholesale or not special or not card:
            return
        for line in self:
            product = line.product_id
            if product.type != "product":
                continue
            qty = line.product_uom_qty or 1
            if line.line_pricelist_id.id != wholesale.id:
                continue
            result = wholesale._compute_price_rule(product, qty)
            price, rule_id = result.get(product.id, (0.0, False))
            if not rule_id or not price > 0:
                line.line_pricelist_id = special
            else:
                line.line_pricelist_id = wholesale

    def _recompute_prices(self):
        for line in self:
            result = line.line_pricelist_id._compute_price_rule(
                line.product_id, line.product_uom_qty
            )
            price, rule_id = result.get(line.product_id.id, (0.0, False))
            line.price_unit = price

    @api.depends("product_id", "product_uom", "product_uom_qty", "line_pricelist_id")
    def _compute_price_unit(self):
        last_prices = {}
        for line in self:
            last_prices[line.id] = line.price_unit
        res = super()._compute_price_unit()
        for line in self:
            if line.is_special_pricelist:
                line._recompute_prices()
            else:
                line.price_unit = last_prices.get(line.id, line.price_unit)
        return res
