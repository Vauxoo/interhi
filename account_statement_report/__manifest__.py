{
    "name": "Estado de cuenta de clientes",
    "summary": "Reporte de antigüedad de saldos por vendedor y por cliente (PDF y Excel)",
    "description": """
        Asistente para generar el estado de cuenta de clientes (facturas con
        saldo pendiente) con días de atraso a una fecha de corte elegida.

        - Reporte de todos los vendedores, con sus clientes agrupados.
        - Reporte de un vendedor en particular, con sus clientes agrupados.
        - Salida en PDF (QWeb) y en Excel (xlsx).
        - Seguridad: un vendedor solo puede generar el reporte de sus propios
          clientes; quienes pertenecen al grupo de gestión ven a todos.
        - Envío recurrente por correo (diario/semanal) a cada vendedor con sus
          clientes, y reporte global a un administrador.
    """,
    "author": "Alvaro Gutierrez",
    "category": "Accounting/Reporting",
    "version": "16.0.2.0.0",
    "depends": ["account", "mail"],
    "data": [
        "security/account_statement_security.xml",
        "security/ir.model.access.csv",
        "data/account_statement_data.xml",
        "report/account_statement_report.xml",
        "views/account_statement_wizard_views.xml",
        "views/account_statement_config_views.xml",
    ],
    "license": "LGPL-3",
    "installable": True,
    "application": False,
}
