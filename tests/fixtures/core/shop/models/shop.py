from odoo import api, fields, models

ORDER_STATE = [('draft', 'Draft'), ('done', 'Done')]


class ShopOrder(models.Model):
    _name = 'shop.order'
    _description = 'Shop Order'

    name = fields.Char('Reference')
    state = fields.Selection(ORDER_STATE, default='draft')
    line_ids = fields.One2many('shop.order.line', 'order_id', string='Lines')
    amount_total = fields.Float(compute='_compute_amount_total', store=True)
    partner_city = fields.Char(related='partner_id.city')
    partner_id = fields.Many2one('shop.partner')

    @api.depends('line_ids.subtotal')
    def _compute_amount_total(self):
        for order in self:
            order.amount_total = sum(order.line_ids.mapped('subtotal'))

    def action_confirm(self):
        self.write(self._prepare_confirm_values())

    def _prepare_confirm_values(self):
        return {'state': 'done'}


class ShopOrderLine(models.Model):
    _name = 'shop.order.line'
    _description = 'Shop Order Line'

    order_id = fields.Many2one('shop.order')
    qty = fields.Float()
    price = fields.Float()
    subtotal = fields.Float(compute='_compute_subtotal', store=True)

    @api.depends('qty', 'price')
    def _compute_subtotal(self):
        for line in self:
            line.subtotal = line.qty * line.price


class ShopPartner(models.Model):
    _name = 'shop.partner'
    _description = 'Shop Partner'

    name = fields.Char()
    city = fields.Char()
