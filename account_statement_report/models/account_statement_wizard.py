# -*- coding: utf-8 -*-
import base64
import datetime
import io
import logging
from collections import OrderedDict

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)

try:
    import xlsxwriter
except ImportError:  # pragma: no cover
    xlsxwriter = None

GROUP_ALL = "account_statement_report.group_account_statement_all"

# Semáforo de atraso: (límite superior de días, color de fondo)
AGING_COLORS = (
    (0, ""),           # al corriente / por vencer (días <= 0): sin color
    (30, "#FFEB9C"),   # 1 - 30 días
    (60, "#F8CBAD"),   # 31 - 60 días
    (None, "#FFC7CE"),  # más de 60 días
)


def _aging_color(days):
    for limit, color in AGING_COLORS:
        if limit is None or days <= limit:
            return color
    return AGING_COLORS[-1][1]


class AccountStatementWizard(models.TransientModel):
    _name = "account.statement.wizard"
    _description = "Asistente de estado de cuenta de clientes"

    statement_date = fields.Date(
        string="Fecha de estado de cuenta",
        required=True,
        default=fields.Date.context_today,
        help="Fecha de corte usada para calcular los días de atraso.",
    )
    report_scope = fields.Selection(
        [("all", "Todos los vendedores"), ("single", "Un vendedor")],
        string="Alcance",
        required=True,
        default="all",
    )
    salesperson_id = fields.Many2one(
        "res.users",
        string="Vendedor",
        domain="[('share', '=', False)]",
    )
    only_overdue = fields.Boolean(
        string="Solo facturas vencidas",
        help="Incluir únicamente facturas que ya pasaron su fecha de "
        "vencimiento en la fecha de corte (días de atraso mayores a 0).",
    )
    min_overdue_days = fields.Integer(
        string="Días de atraso mínimo",
        default=0,
        help="Mostrar solo facturas con al menos estos días de atraso. "
        "Deja 0 para no filtrar por antigüedad.",
    )
    order_by = fields.Selection(
        [
            ("days_desc", "Días de atraso (mayor primero)"),
            ("amount_desc", "Monto (mayor primero)"),
            ("date", "Fecha de factura"),
        ],
        string="Ordenar por",
        required=True,
        default="days_desc",
    )
    output_format = fields.Selection(
        [("pdf", "PDF"), ("xlsx", "Excel")],
        string="Formato",
        required=True,
        default="pdf",
    )
    # Valor por defecto (no computado) para que esté disponible al abrir el
    # asistente, en default_get, antes de renderizar el formulario. Así los
    # attrs de la vista no "parpadean" hasta después de pulsar el botón.
    can_see_all = fields.Boolean(
        default=lambda self: self.env.user.has_group(GROUP_ALL),
    )

    xlsx_file = fields.Binary(string="Archivo Excel", readonly=True)
    xlsx_filename = fields.Char(readonly=True)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if not self.env.user.has_group(GROUP_ALL):
            res.update(report_scope="single", salesperson_id=self.env.user.id)
        return res

    @api.onchange("report_scope")
    def _onchange_report_scope(self):
        if self.report_scope == "single" and not self.salesperson_id:
            self.salesperson_id = self.env.user

    # ------------------------------------------------------------------
    # Seguridad y obtención de datos
    # ------------------------------------------------------------------
    def _check_access_scope(self):
        """Impide que un vendedor saque el reporte de otros vendedores/clientes."""
        self.ensure_one()
        if self.can_see_all:
            return
        if self.report_scope == "all" or (
            self.salesperson_id and self.salesperson_id != self.env.user
        ):
            raise AccessError(
                _("Solo puedes generar el estado de cuenta de tus propios clientes.")
            )

    def _get_moves(self):
        self.ensure_one()
        domain = [
            ("move_type", "in", ("out_invoice", "out_refund")),
            ("state", "=", "posted"),
            ("amount_residual", ">", 0),
        ]
        if not self.can_see_all:
            # Vendedor restringido: forzado a sus propias facturas.
            domain.append(("invoice_user_id", "=", self.env.user.id))
        elif self.report_scope == "single" and self.salesperson_id:
            domain.append(("invoice_user_id", "=", self.salesperson_id.id))
        return self.env["account.move"].search(
            domain, order="invoice_user_id, partner_id, invoice_date, name"
        )

    def _line_sort_key(self):
        return {
            "days_desc": lambda l: (-l["dias"], -l["monto"]),
            "amount_desc": lambda l: (-l["monto"], -l["dias"]),
            "date": lambda l: (l["fecha_fact"] or datetime.date.min, l["factura"] or ""),
        }[self.order_by]

    def _build_report_data(self):
        self.ensure_one()
        self._check_access_scope()
        cutoff = self.statement_date
        groups = OrderedDict()
        count = 0
        for move in self._get_moves():
            due = move.invoice_date_due
            days = (cutoff - due).days if due else 0
            if self.only_overdue and days <= 0:
                continue
            if self.min_overdue_days and days < self.min_overdue_days:
                continue
            sp = move.invoice_user_id
            group = groups.setdefault(
                sp.id or 0,
                {
                    "name": sp.name or _("Sin vendedor"),
                    "clients": OrderedDict(),
                    "subtotal": 0.0,
                },
            )
            partner = move.partner_id
            client = group["clients"].setdefault(
                partner.id,
                {"name": partner.name or "", "lines": [], "subtotal": 0.0},
            )
            amount = move.amount_residual_signed
            client["lines"].append(
                {
                    "factura": move.name,
                    "fecha_fact": move.invoice_date,
                    "fecha_venc": due,
                    "fecha_corte": cutoff,
                    "dias": days,
                    "color": _aging_color(days),
                    "monto": amount,
                }
            )
            client["subtotal"] += amount
            group["subtotal"] += amount
            count += 1

        sort_key = self._line_sort_key()
        for group in groups.values():
            for client in group["clients"].values():
                client["lines"].sort(key=sort_key)

        total = sum(g["subtotal"] for g in groups.values())
        return {
            "company": self.env.company,
            "currency": self.env.company.currency_id,
            "cutoff": cutoff,
            "user": self.env.user,
            "print_date": fields.Date.context_today(self),
            "scope": "single" if not self.can_see_all else self.report_scope,
            "only_overdue": self.only_overdue,
            "min_overdue_days": self.min_overdue_days,
            "order_label": dict(
                self._fields["order_by"]._description_selection(self.env)
            ).get(self.order_by, ""),
            "groups": list(groups.values()),
            "total": total,
            "invoice_count": count,
        }

    # ------------------------------------------------------------------
    # Acciones
    # ------------------------------------------------------------------
    def action_generate(self):
        self.ensure_one()
        self._check_access_scope()
        if self.output_format == "xlsx":
            return self.action_generate_xlsx()
        return self.action_generate_pdf()

    def action_generate_pdf(self):
        self.ensure_one()
        return self.env.ref(
            "account_statement_report.action_report_account_statement"
        ).report_action(self)

    def _report_basename(self):
        self.ensure_one()
        who = self.salesperson_id.name if self.report_scope == "single" and self.salesperson_id else "Todos"
        slug = "".join(c if c.isalnum() else "_" for c in who)
        return "Estado_de_cuenta_%s_%s" % (slug, self.statement_date or "")

    def _render_xlsx_bytes(self, data=None):
        """Devuelve los bytes del reporte en formato XLSX."""
        self.ensure_one()
        if xlsxwriter is None:
            raise UserError(_("La librería xlsxwriter no está disponible en el servidor."))
        if data is None:
            data = self._build_report_data()
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {"in_memory": True})
        self._write_xlsx(workbook, data)
        workbook.close()
        output.seek(0)
        return output.read()

    def _render_pdf_bytes(self):
        """Devuelve los bytes del reporte en formato PDF."""
        self.ensure_one()
        return self.env["ir.actions.report"]._render_qweb_pdf(
            "account_statement_report.action_report_account_statement",
            res_ids=self.ids,
        )[0]

    def action_generate_xlsx(self):
        self.ensure_one()
        filename = "%s.xlsx" % self._report_basename()
        self.write(
            {
                "xlsx_file": base64.b64encode(self._render_xlsx_bytes()),
                "xlsx_filename": filename,
            }
        )
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/account.statement.wizard/%s/xlsx_file/%s?download=true"
            % (self.id, filename),
            "target": "self",
        }

    def _write_xlsx(self, workbook, data):
        money_fmt = "$ #,##0.00"
        title = workbook.add_format({"bold": True, "font_size": 14})
        sub = workbook.add_format({"font_size": 10})
        sp_fmt = workbook.add_format(
            {"bold": True, "font_size": 11, "bg_color": "#D9E1F2"}
        )
        header = workbook.add_format(
            {
                "bold": True,
                "align": "center",
                "valign": "vcenter",
                "text_wrap": True,
                "bg_color": "#BFBFBF",
                "border": 1,
            }
        )
        cell = workbook.add_format({"border": 1})
        cell_center = workbook.add_format({"border": 1, "align": "center"})
        cell_money = workbook.add_format({"border": 1, "num_format": money_fmt})
        cli_sub_lbl = workbook.add_format(
            {"bold": True, "border": 1, "align": "right"}
        )
        cli_sub_money = workbook.add_format(
            {"bold": True, "border": 1, "num_format": money_fmt}
        )
        sp_sub_lbl = workbook.add_format(
            {"bold": True, "border": 1, "align": "right", "bg_color": "#D9E1F2"}
        )
        sp_sub_money = workbook.add_format(
            {"bold": True, "border": 1, "num_format": money_fmt, "bg_color": "#D9E1F2"}
        )
        total_lbl = workbook.add_format(
            {"bold": True, "border": 1, "align": "right", "bg_color": "#FFE699"}
        )
        total_money = workbook.add_format(
            {"bold": True, "border": 1, "num_format": money_fmt, "bg_color": "#FFE699"}
        )
        # Semáforo de atraso: un formato por color (los días <= 0 van sin relleno).
        aging_fmts = {
            color: workbook.add_format(
                {"border": 1, "align": "center", "bg_color": color}
            )
            for _limit, color in AGING_COLORS
            if color
        }

        sheet = workbook.add_worksheet("Estado de cuenta")
        sheet.set_column("A:A", 12)
        sheet.set_column("B:B", 38)
        sheet.set_column("C:E", 14)
        sheet.set_column("F:F", 11)
        sheet.set_column("G:G", 16)

        sheet.write(0, 0, "Estado de cuenta de clientes", title)
        sheet.write(1, 0, data["company"].name or "", sub)
        sheet.write(2, 0, "Fecha de corte: %s" % (data["cutoff"] or ""), sub)
        filtros = []
        if data.get("only_overdue"):
            filtros.append("solo vencidas")
        if data.get("min_overdue_days"):
            filtros.append("atraso ≥ %s días" % data["min_overdue_days"])
        filtros.append("orden: %s" % (data.get("order_label") or ""))
        sheet.write(3, 0, "Filtros: %s" % " · ".join(filtros), sub)
        row = 5

        columns = [
            "FACTURA",
            "CLIENTE",
            "FECHA FACT",
            "FECHA VENC.",
            "FECHA DE CORTE",
            "DIAS DE ATRASO",
            "MONTO",
        ]

        for group in data["groups"]:
            sheet.merge_range(row, 0, row, 6, group["name"], sp_fmt)
            row += 1
            for col, label in enumerate(columns):
                sheet.write(row, col, label, header)
            row += 1
            for client in group["clients"].values():
                for line in client["lines"]:
                    sheet.write(row, 0, line["factura"] or "", cell)
                    sheet.write(row, 1, client["name"], cell)
                    sheet.write(row, 2, self._d(line["fecha_fact"]), cell_center)
                    sheet.write(row, 3, self._d(line["fecha_venc"]), cell_center)
                    sheet.write(row, 4, self._d(line["fecha_corte"]), cell_center)
                    sheet.write_number(
                        row, 5, line["dias"], aging_fmts.get(line["color"], cell_center)
                    )
                    sheet.write_number(row, 6, line["monto"], cell_money)
                    row += 1
                sheet.merge_range(
                    row, 0, row, 5,
                    "Total %s" % client["name"], cli_sub_lbl,
                )
                sheet.write_number(row, 6, client["subtotal"], cli_sub_money)
                row += 1
            sheet.merge_range(
                row, 0, row, 5,
                "Total vendedor %s" % group["name"], sp_sub_lbl,
            )
            sheet.write_number(row, 6, group["subtotal"], sp_sub_money)
            row += 2

        sheet.merge_range(row, 0, row, 5, "TOTAL GENERAL", total_lbl)
        sheet.write_number(row, 6, data["total"], total_money)

    @staticmethod
    def _d(value):
        return value.strftime("%Y-%m-%d") if value else ""

    # ------------------------------------------------------------------
    # Envío recurrente por correo (acción planificada)
    # ------------------------------------------------------------------
    def _statement_recipients(self):
        """Vendedores a los que se les envía el reporte (según configuración)."""
        Param = self.env["ir.config_parameter"].sudo()
        ids_raw = Param.get_param("account_statement_report.salesperson_ids", "")
        ids = [int(i) for i in ids_raw.split(",") if i.strip().isdigit()]
        if ids:
            users = self.env["res.users"].browse(ids).exists()
        else:
            users = self.env["res.users"].search(
                [("share", "=", False), ("active", "=", True)]
            )
        return users.filtered(lambda u: u.email)

    def _send_statement_email(self, wizard, data):
        """Genera PDF + XLSX para un wizard y envía el correo con ambos adjuntos."""
        if wizard.report_scope == "single" and wizard.salesperson_id:
            email_to = wizard.salesperson_id.email
        else:
            email_to = wizard.env.context.get("statement_email_to")
        if not email_to:
            return False
        template = self.env.ref(
            "account_statement_report.mail_template_account_statement",
            raise_if_not_found=False,
        )
        if not template:
            return False
        basename = wizard._report_basename()
        attachments = self.env["ir.attachment"].create(
            [
                {
                    "name": "%s.pdf" % basename,
                    "datas": base64.b64encode(wizard._render_pdf_bytes()),
                    "mimetype": "application/pdf",
                },
                {
                    "name": "%s.xlsx" % basename,
                    "datas": base64.b64encode(wizard._render_xlsx_bytes(data)),
                    "mimetype": "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet",
                },
            ]
        )
        template.send_mail(
            wizard.id,
            force_send=True,
            email_values={
                "email_to": email_to,
                "attachment_ids": [(6, 0, attachments.ids)],
            },
        )
        return True

    @api.model
    def _cron_send_statements(self):
        """Acción planificada: envía a cada vendedor su estado de cuenta (PDF+XLSX)
        y, opcionalmente, un reporte global a un administrador."""
        Param = self.env["ir.config_parameter"].sudo()
        frequency = Param.get_param("account_statement_report.frequency", "weekly")
        cutoff = fields.Date.context_today(self)
        if frequency == "weekly":
            weekday = int(Param.get_param("account_statement_report.weekday", "0") or 0)
            if cutoff.weekday() != weekday:
                return
        Wizard = self.with_context(active_test=False)
        sent = skipped = 0

        # Un correo por vendedor con sus propios clientes.
        for user in self._statement_recipients():
            wiz = Wizard.create(
                {
                    "statement_date": cutoff,
                    "report_scope": "single",
                    "salesperson_id": user.id,
                    "order_by": "days_desc",
                }
            )
            data = wiz._build_report_data()
            if self._send_statement_email(wiz, data):
                sent += 1
            else:
                skipped += 1

        # Reporte global para el administrador configurado.
        admin_id = Param.get_param("account_statement_report.admin_user_id", "")
        admin = (
            self.env["res.users"].browse(int(admin_id)).exists()
            if admin_id.isdigit()
            else self.env["res.users"]
        )
        if admin and admin.email:
            wiz = Wizard.create(
                {
                    "statement_date": cutoff,
                    "report_scope": "all",
                    "order_by": "days_desc",
                }
            )
            data = wiz._build_report_data()
            self._send_statement_email(
                wiz.with_context(statement_email_to=admin.email), data
            )
            sent += 1

        _logger.info(
            "Estado de cuenta recurrente: %s correos enviados, %s omitidos (sin email).",
            sent,
            skipped,
        )
