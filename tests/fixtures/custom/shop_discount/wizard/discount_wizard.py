from odoo import fields, models


class ShopDiscountWizard(models.TransientModel):
    _name = 'shop.discount.wizard'
    _description = 'Set Discount'

    order_id = fields.Many2one('shop.order')
    discount = fields.Float()

    def action_apply(self):
        self.env['shop.order'].browse(self.order_id.id).write({'discount': self.discount})
