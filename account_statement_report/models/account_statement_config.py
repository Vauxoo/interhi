# -*- coding: utf-8 -*-
from odoo import api, fields, models

CRON_XMLID = "account_statement_report.ir_cron_send_statements"
PARAM_FREQ = "account_statement_report.frequency"
PARAM_WEEKDAY = "account_statement_report.weekday"
PARAM_ADMIN = "account_statement_report.admin_user_id"
PARAM_SALES = "account_statement_report.salesperson_ids"


class AccountStatementConfig(models.TransientModel):
    _name = "account.statement.config"
    _description = "Configuración del envío recurrente del estado de cuenta"

    auto_send = fields.Boolean(
        string="Activar envío recurrente",
        help="Activa la acción planificada que envía por correo el estado de "
        "cuenta a cada vendedor (PDF + Excel).",
    )
    frequency = fields.Selection(
        [("daily", "Diario"), ("weekly", "Semanal")],
        string="Frecuencia",
        required=True,
        default="weekly",
    )
    weekday = fields.Selection(
        [
            ("0", "Lunes"),
            ("1", "Martes"),
            ("2", "Miércoles"),
            ("3", "Jueves"),
            ("4", "Viernes"),
            ("5", "Sábado"),
            ("6", "Domingo"),
        ],
        string="Día de envío",
        default="0",
        help="Día de la semana en que se envía cuando la frecuencia es semanal.",
    )
    admin_user_id = fields.Many2one(
        "res.users",
        string="Administrador (reporte global)",
        domain="[('share', '=', False)]",
        help="Recibe un correo aparte con el estado de cuenta de TODOS los "
        "vendedores. Vacío = no se envía el reporte global.",
    )
    salesperson_ids = fields.Many2many(
        "res.users",
        string="Vendedores a incluir",
        domain="[('share', '=', False)]",
        help="Vendedores que reciben su propio reporte. Vacío = todos los "
        "usuarios internos activos con correo.",
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        Param = self.env["ir.config_parameter"].sudo()
        cron = self.env.ref(CRON_XMLID, raise_if_not_found=False)
        admin_id = Param.get_param(PARAM_ADMIN, "")
        sales_raw = Param.get_param(PARAM_SALES, "")
        sales_ids = [int(i) for i in sales_raw.split(",") if i.strip().isdigit()]
        res.update(
            auto_send=bool(cron and cron.active),
            frequency=Param.get_param(PARAM_FREQ, "weekly"),
            weekday=Param.get_param(PARAM_WEEKDAY, "0"),
            admin_user_id=int(admin_id) if admin_id.isdigit() else False,
            salesperson_ids=[(6, 0, sales_ids)],
        )
        return res

    def action_save(self):
        self.ensure_one()
        Param = self.env["ir.config_parameter"].sudo()
        cron = self.env.ref(CRON_XMLID, raise_if_not_found=False)
        if cron:
            cron.active = self.auto_send
        Param.set_param(PARAM_FREQ, self.frequency)
        Param.set_param(PARAM_WEEKDAY, self.weekday or "0")
        Param.set_param(
            PARAM_ADMIN, str(self.admin_user_id.id) if self.admin_user_id else ""
        )
        Param.set_param(
            PARAM_SALES, ",".join(str(i) for i in self.salesperson_ids.ids)
        )
        return {"type": "ir.actions.act_window_close"}
