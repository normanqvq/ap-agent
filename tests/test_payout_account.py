from apagent.schemas import Document, DocType, LineItem, Vendor


def _line():
    return LineItem(
        line_no=1, sku="SKU-1", description="widget",
        qty=10, uom="PCS", unit_price_cents=100, line_total_cents=1000,
    )


def test_document_carries_payout_account_defaulting_none():
    doc = Document(
        doc_id="INV1", doc_type=DocType.INVOICE, vendor_id="V1",
        vendor_name="Acme", issue_date="2026-01-01", ref_doc_id="PO1",
        currency="SGD", lines=[_line()],
    )
    assert doc.payout_account is None
    doc2 = doc.model_copy(update={"payout_account": "SG12 3456"})
    assert doc2.payout_account == "SG12 3456"


def test_vendor_master_model():
    v = Vendor(vendor_id="V1", vendor_name="Acme", payout_account="SG12 3456")
    assert v.vendor_id == "V1"
    assert v.payout_account == "SG12 3456"
    assert Vendor(vendor_id="V2", vendor_name="B").payout_account is None
