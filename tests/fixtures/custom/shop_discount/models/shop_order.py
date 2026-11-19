from odoo import api, fields, models
from odoo.exceptions import ValidationError

NET_DEPENDS = ('amount_total', 'discount')


class ShopOrder(models.Model):
    _inherit = 'shop.order'

    discount = fields.Float('Discount (%)')
    amount_net = fields.Float('Net Amount', compute='_compute_amount_net', store=True)
    note = fields.Char()
    state = fields.Selection(selection_add=[('sent', 'Sent'), ('done',)])

    @api.depends(*NET_DEPENDS)
    def _compute_amount_net(self):
        for order in self:
            order.amount_net = order.amount_total * (1 - order.discount / 100)

    @api.constrains('discount')
    def _check_discount(self):
        if any(o.discount > 50 for o in self):
            raise ValidationError('Too much')

    def write(self, vals):
        if 'discount' in vals:
            vals['note'] = 'discount changed'
        return super().write(vals)

    def action_apply_discount(self):
        self.write({'discount': 10})

    def action_open_discount_wizard(self):
        return {'type': 'ir.actions.act_window', 'res_model': 'shop.discount.wizard', 'target': 'new'}

    def _cron_reset_discount(self):
        for order in self.search([]):
            order.discount = 0
            order.state = 'draft'

    def action_send(self):
        for order in self:
            order.state = 'sent'
