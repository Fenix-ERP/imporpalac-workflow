{
    "name": "Imporpalac Core",
    "version": "17.0.1.0.0",
    "category": "Accounting/Localization",
    "summary": """Core module for the Imporpalac system.""",
    "author": "Daniela Lagla / FenixERP",
    "website": "https://github.com/Fenix-ERP/l10n-ecuador",
    "depends": ["stock"],
    "data": [
        "views/stock_picking_inherit.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "imporpalac_core/static/src/core/web/**/*",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
    "license": "OPL-1",
}
