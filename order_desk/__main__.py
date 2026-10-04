import argparse
import json
from pathlib import Path
import sys
import tempfile
from . import OrderDesk

ACTIONS = {'add-product': 'add_product', 'set-product-enabled': 'set_product_enabled', 'get-product': 'get_product', 'reprice-products': 'reprice_products', 'restock': 'restock', 'stock': 'stock', 'place': 'place', 'amend': 'amend', 'reduce-order': 'reduce_order', 'reserve-order': 'reserve_order', 'reserve-batch': 'reserve_batch', 'quote': 'quote', 'quote-amend': 'quote_amend', 'get': 'get', 'cancel': 'cancel', 'cancel-batch': 'cancel_batch', 'reopen-order': 'reopen_order', 'ship': 'ship', 'ship-batch': 'ship_batch', 'confirm-delivery': 'confirm_delivery', 'confirm-shipment-delivery': 'confirm_shipment_delivery', 'correct-shipment-delivery': 'correct_shipment_delivery', 'revoke-shipment-delivery': 'revoke_shipment_delivery', 'correct-shipment': 'correct_shipment', 'list': 'list_orders', 'quote-return': 'quote_return', 'record-return': 'record_return', 'cancel-return': 'cancel_return', 'amend-return': 'amend_return', 'split-return': 'split_return', 'returns': 'get_returns', 'receive-return': 'receive_return', 'receive-return-batch': 'receive_return_batch', 'cancel-received-return': 'cancel_received_return', 'return-receipt': 'get_return_receipt', 'return-worklist': 'return_worklist', 'order-progress': 'order_progress', 'order-relations': 'order_relations', 'shipment-orders': 'shipment_orders', 'fulfillment-summary': 'fulfillment_summary', 'order-worklist': 'order_worklist', 'shipment-worklist': 'shipment_worklist', 'replenishment-report': 'replenishment_report', 'history': 'history', 'stock-history': 'stock_history', 'pick-list': 'pick_list', 'count-stock': 'count_stock', 'stock-count': 'get_stock_count', 'reverse-stock-count': 'reverse_stock_count', 'stock-count-reversal': 'get_stock_count_reversal', 'reservation-plan': 'reservation_plan', 'reservation-audit': 'reservation_audit', 'transfer-reservation': 'transfer_reservation', 'release-reservation': 'release_reservation', 'merge-orders': 'merge_orders', 'split-order': 'split_order', 'save-cart': 'save_cart', 'cart': 'get_cart', 'checkout-cart': 'checkout_cart', 'checkout-cart-part': 'checkout_cart_part', 'checkout-cart-batch': 'checkout_cart_batch'}

def samples(name):
    return json.loads((Path(__file__).resolve().parent.parent / "examples" / name).read_text(encoding="utf-8"))

def demo(app):
    for product in samples("products.json"):
        app.add_product(**product)
    return app.place(**samples("order.json"))

def main(argv=None):
    parser = argparse.ArgumentParser(description="订单工作台")
    parser.add_argument("--root", required=True, help="local data directory")
    parser.add_argument("action", choices=[*ACTIONS, "demo"])
    parser.add_argument("input", nargs="?", help="UTF-8 JSON object, or array of objects, containing API arguments")
    args = parser.parse_args(argv)
    try:
        if args.action == "demo":
            # Sample operations run in a fresh child directory and never overwrite user's data.
            Path(args.root).mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="sample-", dir=args.root) as location:
                value = demo(OrderDesk(location))
        else:
            payload = json.loads(Path(args.input).read_text(encoding="utf-8")) if args.input else {}
            app = OrderDesk(args.root)
            method = getattr(app, ACTIONS[args.action])
            if isinstance(payload, list):
                value = []
                for row in payload:
                    if not isinstance(row, dict):
                        raise ValueError("each input must be an object")
                    value.append(method(**row))
            elif isinstance(payload, dict):
                value = method(**payload)
            else:
                raise ValueError("input must be an object or array")
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
