import copy
from .storage import JsonStore, text, positive

class OrderDesk(JsonStore):
    def _record_event(self, data, order_id, action, result, complete):
        # Each order counts its own events from 1; legacy orders start with
        # complete=False the first time a new operation is recorded.
        document = data.setdefault("history", {}).setdefault(
            order_id, {"complete": complete, "events": []}
        )
        document["events"].append({
            "sequence": len(document["events"]) + 1,
            "action": action,
            "result": copy.deepcopy(result),
        })

    def add_product(self, sku, name, price_cents):
        sku, name = text(sku, "sku"), text(name, "name")
        if type(price_cents) is not int or price_cents < 0:
            raise ValueError("price_cents must be a nonnegative integer")
        data = self._read()
        products = data.setdefault("products", {})
        if sku in products:
            raise ValueError("product already exists")
        # New products sell by default; the enabled flag is only stored once a
        # set_product_enabled call needs it, so a missing flag reads as enabled.
        product = {"sku": sku, "name": name, "price_cents": price_cents}
        products[sku] = product
        self._write(data)
        return product

    @staticmethod
    def _is_enabled(product):
        return product.get("enabled") is not False

    @staticmethod
    def _product_view(product):
        return {
            "sku": product["sku"],
            "name": product["name"],
            "price_cents": product["price_cents"],
            "enabled": product.get("enabled") is not False,
        }

    def set_product_enabled(self, sku, enabled):
        sku = text(sku, "sku")
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        data = self._read()
        product = data.get("products", {}).get(sku)
        if product is None:
            raise ValueError("unknown product: " + sku)
        product["enabled"] = enabled
        self._write(data)
        return self._product_view(product)

    def get_product(self, sku):
        sku = text(sku, "sku")
        product = self._read().get("products", {}).get(sku)
        if product is None:
            raise ValueError("unknown product: " + sku)
        # Read-only: a missing enabled flag means enabled and is never filled in.
        return self._product_view(product)

    def restock(self, sku, quantity):
        sku = text(sku, "sku")
        quantity = positive(quantity, "quantity")
        data = self._read()
        if sku not in data.get("products", {}):
            raise ValueError("unknown product: " + sku)
        inventory = data.setdefault("inventory", {})
        entry = inventory.setdefault(sku, {"on_hand": 0, "reserved": 0})
        entry["on_hand"] += quantity
        self._write(data)
        return self._stock_view(sku, entry)

    def stock(self, sku):
        sku = text(sku, "sku")
        data = self._read()
        if sku not in data.get("products", {}):
            raise ValueError("unknown product: " + sku)
        entry = data.get("inventory", {}).get(sku)
        if entry is None:
            return {"sku": sku, "on_hand": None, "reserved": 0, "available": None}
        return self._stock_view(sku, entry)

    @staticmethod
    def _stock_view(sku, entry):
        on_hand = entry["on_hand"]
        reserved = entry["reserved"]
        return {"sku": sku, "on_hand": on_hand, "reserved": reserved, "available": on_hand - reserved}

    def count_stock(self, count_id, lines):
        count_id = text(count_id, "count_id")
        if not isinstance(lines, list) or not lines:
            raise ValueError("lines must be a nonempty list")
        counts = {}
        for line in lines:
            if not isinstance(line, dict):
                raise ValueError("each line must be an object with sku and on_hand")
            sku = text(line.get("sku"), "sku")
            on_hand = line.get("on_hand")
            if type(on_hand) is not int or on_hand < 0:
                raise ValueError("on_hand must be a nonnegative integer")
            if sku in counts:
                raise ValueError("duplicate sku in count: " + sku)
            counts[sku] = on_hand
        data = self._read()
        if count_id in data.get("stock_counts", {}):
            raise ValueError("stock count already exists: " + count_id)
        products = data.get("products", {})
        inventory = data.get("inventory", {})
        # Validate every line and build the snapshot before touching any
        # inventory entry, so a rejected count leaves stock untouched.
        result_lines = []
        for sku in sorted(counts):
            if sku not in products:
                raise ValueError("unknown product: " + sku)
            entry = inventory.get(sku)
            if entry is None:
                raise ValueError("product is not managed: " + sku)
            on_hand = counts[sku]
            reserved = entry["reserved"]
            if on_hand < reserved:
                raise ValueError("on_hand cannot be below reserved quantity: " + sku)
            before = self._stock_view(sku, entry)
            after = {"sku": sku, "on_hand": on_hand, "reserved": reserved, "available": on_hand - reserved}
            result_lines.append({
                "sku": sku,
                "before": before,
                "after": after,
                "delta": on_hand - before["on_hand"],
            })
        for sku, on_hand in counts.items():
            inventory[sku]["on_hand"] = on_hand
        result = {"count_id": count_id, "lines": result_lines}
        data.setdefault("stock_counts", {})[count_id] = copy.deepcopy(result)
        self._write(data)
        return result

    def get_stock_count(self, count_id):
        count_id = text(count_id, "count_id")
        record = self._read().get("stock_counts", {}).get(count_id)
        if record is None:
            raise ValueError("unknown stock count: " + count_id)
        return copy.deepcopy(record)

    def place(self, order_id, lines):
        order_id = text(order_id, "order_id")
        if not isinstance(lines, list) or not lines:
            raise ValueError("lines must be a nonempty list")
        data = self._read()
        order = self._place(data, order_id, lines)
        self._write(data)
        return order

    def _place(self, data, order_id, lines):
        # Shared by place and checkout_cart: validates against the current
        # catalog and inventory inside `data`, reserves stock and records the
        # place event. Caller writes `data` once everything has succeeded.
        if order_id in data.get("orders", {}):
            raise ValueError("order already exists")
        products = data.get("products", {})
        items = []
        needed = {}
        for line in lines:
            sku, quantity = text(line["sku"], "sku"), positive(line["quantity"], "quantity")
            product = products.get(sku)
            if product is None:
                raise ValueError("unknown product: " + sku)
            if not self._is_enabled(product):
                raise ValueError("product is not available for sale: " + sku)
            items.append({"sku": sku, "quantity": quantity, "unit_price_cents": product["price_cents"], "subtotal_cents": quantity * product["price_cents"]})
            needed[sku] = needed.get(sku, 0) + quantity
        inventory = data.get("inventory", {})
        reservations = {}
        for sku, quantity in needed.items():
            entry = inventory.get(sku)
            if entry is None:
                continue
            if quantity > entry["on_hand"] - entry["reserved"]:
                raise ValueError("insufficient stock: " + sku)
            reservations[sku] = quantity
        if reservations:
            inventory = data.setdefault("inventory", {})
            for sku, quantity in reservations.items():
                inventory[sku]["reserved"] += quantity
            data.setdefault("reservations", {})[order_id] = reservations
        order = {"order_id": order_id, "status": "placed", "lines": items, "total_cents": sum(x["subtotal_cents"] for x in items)}
        data.setdefault("orders", {})[order_id] = order
        self._record_event(data, order_id, "place", order, True)
        return order

    def save_cart(self, cart_id, lines):
        # Carts only remember what to buy: no prices, no reservations, no
        # history. Paused or out-of-stock products may be saved; everything is
        # re-checked against the catalog and stock at checkout time.
        cart_id = text(cart_id, "cart_id")
        if not isinstance(lines, list) or not lines:
            raise ValueError("lines must be a nonempty list")
        requested = {}
        for line in lines:
            if not isinstance(line, dict):
                raise ValueError("each line must be an object with sku and quantity")
            sku = text(line.get("sku"), "sku")
            quantity = positive(line.get("quantity"), "quantity")
            requested[sku] = requested.get(sku, 0) + quantity
        data = self._read()
        products = data.get("products", {})
        for sku in requested:
            if sku not in products:
                raise ValueError("unknown product: " + sku)
        cart = {
            "cart_id": cart_id,
            "lines": [{"sku": sku, "quantity": requested[sku]} for sku in sorted(requested)],
        }
        # A new id creates the cart; an existing id is replaced wholesale.
        data.setdefault("carts", {})[cart_id] = cart
        self._write(data)
        return cart

    def get_cart(self, cart_id):
        cart_id = text(cart_id, "cart_id")
        cart = self._read().get("carts", {}).get(cart_id)
        if cart is None:
            raise ValueError("unknown cart: " + cart_id)
        return copy.deepcopy(cart)

    def checkout_cart(self, cart_id, order_id):
        cart_id = text(cart_id, "cart_id")
        order_id = text(order_id, "order_id")
        data = self._read()
        cart = data.get("carts", {}).get(cart_id)
        if cart is None:
            raise ValueError("unknown cart: " + cart_id)
        # Place against the current catalog and stock; _place validates
        # everything before mutating, so a rejected checkout leaves the cart,
        # orders, inventory and history untouched.
        order = self._place(data, order_id, copy.deepcopy(cart["lines"]))
        data.get("carts", {}).pop(cart_id, None)
        self._write(data)
        return order

    def amend(self, order_id, lines):
        order_id = text(order_id, "order_id")
        if not isinstance(lines, list) or not lines:
            raise ValueError("lines must be a nonempty list")
        requested = []
        needed = {}
        for line in lines:
            if not isinstance(line, dict):
                raise ValueError("each line must be an object with sku and quantity")
            sku = text(line.get("sku"), "sku")
            quantity = positive(line.get("quantity"), "quantity")
            requested.append((sku, quantity))
            needed[sku] = needed.get(sku, 0) + quantity
        data = self._read()
        order = data.get("orders", {}).get(order_id)
        if order is None:
            raise ValueError("unknown order: " + order_id)
        if order["status"] != "placed":
            raise ValueError("only a placed order can be amended")
        # Paused products may stay in an order, shrink or disappear, but no new
        # paused SKU can be added and its merged total may never grow. Compare
        # current ordered quantities against the merged new list -- never
        # history or actual reservations.
        current = {}
        for line in order["lines"]:
            current[line["sku"]] = current.get(line["sku"], 0) + line["quantity"]
        products = data.get("products", {})
        items = []
        for sku, quantity in requested:
            product = products.get(sku)
            if product is None:
                raise ValueError("unknown product: " + sku)
            if not self._is_enabled(product) and (
                sku not in current or needed[sku] > current[sku]
            ):
                raise ValueError("product is not available for sale: " + sku)
            items.append({"sku": sku, "quantity": quantity, "unit_price_cents": product["price_cents"], "subtotal_cents": quantity * product["price_cents"]})
        inventory = data.get("inventory", {})
        own = data.get("reservations", {}).get(order_id, {})
        # New demand may use what is available plus what this order already
        # holds; orders without reservation records get no extra allowance.
        reservations = {}
        for sku, quantity in needed.items():
            entry = inventory.get(sku)
            if entry is None:
                continue
            if quantity > entry["on_hand"] - entry["reserved"] + own.get(sku, 0):
                raise ValueError("insufficient stock: " + sku)
            reservations[sku] = quantity
        # Validate everything before touching inventory, so a rejected amend
        # leaves stock, reservations and history untouched.
        for sku, quantity in own.items():
            entry = inventory.get(sku)
            if entry is not None:
                entry["reserved"] -= quantity
        if reservations:
            for sku, quantity in reservations.items():
                inventory[sku]["reserved"] += quantity
            data.setdefault("reservations", {})[order_id] = reservations
        else:
            data.get("reservations", {}).pop(order_id, None)
        order["lines"] = items
        order["total_cents"] = sum(x["subtotal_cents"] for x in items)
        self._record_event(data, order_id, "amend", order, False)
        self._write(data)
        return order

    def quote(self, lines):
        # Preview only: validate and price against current data, never write.
        if not isinstance(lines, list) or not lines:
            raise ValueError("lines must be a nonempty list")
        requested = {}
        for line in lines:
            if not isinstance(line, dict):
                raise ValueError("each line must be an object with sku and quantity")
            sku = text(line.get("sku"), "sku")
            quantity = positive(line.get("quantity"), "quantity")
            requested[sku] = requested.get(sku, 0) + quantity
        data = self._read()
        products = data.get("products", {})
        inventory = data.get("inventory", {})
        result_lines = []
        can_place = True
        for sku in sorted(requested):
            product = products.get(sku)
            if product is None:
                raise ValueError("unknown product: " + sku)
            if not self._is_enabled(product):
                raise ValueError("product is not available for sale: " + sku)
            quantity = requested[sku]
            unit_price = product["price_cents"]
            entry = inventory.get(sku)
            if entry is None:
                # Legacy data without inventory keys stays unmanaged.
                available = None
                shortfall = 0
            else:
                available = entry["on_hand"] - entry["reserved"]
                shortfall = max(0, quantity - available)
            if shortfall:
                can_place = False
            result_lines.append({
                "sku": sku,
                "quantity": quantity,
                "unit_price_cents": unit_price,
                "subtotal_cents": quantity * unit_price,
                "available": available,
                "shortfall": shortfall,
            })
        return {
            "lines": result_lines,
            "total_cents": sum(line["subtotal_cents"] for line in result_lines),
            "can_place": can_place,
        }

    def get(self, order_id):
        try:
            return self._read().get("orders", {})[order_id]
        except KeyError:
            raise ValueError("unknown order: " + order_id) from None

    def cancel(self, order_id):
        data = self._read()
        order = data.get("orders", {}).get(order_id)
        if order is None or order["status"] != "placed":
            raise ValueError("only a placed order can be cancelled")
        reservations = data.get("reservations", {}).pop(order_id, None)
        if reservations:
            inventory = data.get("inventory", {})
            for sku, quantity in reservations.items():
                entry = inventory.get(sku)
                if entry is not None:
                    entry["reserved"] -= quantity
        order["status"] = "cancelled"
        self._record_event(data, order_id, "cancel", order, False)
        self._write(data)
        return order

    def ship(self, order_id, carrier, tracking_no):
        order_id = text(order_id, "order_id")
        carrier = text(carrier, "carrier")
        tracking_no = text(tracking_no, "tracking_no")
        data = self._read()
        order = data.get("orders", {}).get(order_id)
        if order is None:
            raise ValueError("unknown order: " + order_id)
        if order["status"] == "cancelled":
            raise ValueError("a cancelled order cannot be shipped")
        if order["status"] == "shipped":
            raise ValueError("order already shipped")
        if order["status"] != "placed":
            raise ValueError("only a placed order can be shipped")
        reservations = data.get("reservations", {}).pop(order_id, None)
        if reservations:
            inventory = data.get("inventory", {})
            for sku, quantity in reservations.items():
                entry = inventory.get(sku)
                if entry is not None:
                    entry["on_hand"] -= quantity
                    entry["reserved"] -= quantity
        order["status"] = "shipped"
        order["shipment"] = {"carrier": carrier, "tracking_no": tracking_no}
        self._record_event(data, order_id, "ship", order, False)
        self._write(data)
        return order

    def list_orders(self):
        return sorted(self._read().get("orders", {}).values(), key=lambda x: x["order_id"])

    def record_return(self, order_id, return_id, lines):
        order_id = text(order_id, "order_id")
        return_id = text(return_id, "return_id")
        if not isinstance(lines, list) or not lines:
            raise ValueError("lines must be a nonempty list")
        requested = {}
        for line in lines:
            if not isinstance(line, dict):
                raise ValueError("each line must be an object with sku and quantity")
            sku = text(line.get("sku"), "sku")
            quantity = positive(line.get("quantity"), "quantity")
            requested[sku] = requested.get(sku, 0) + quantity
        data = self._read()
        order = data.get("orders", {}).get(order_id)
        if order is None:
            raise ValueError("unknown order: " + order_id)
        if order["status"] != "shipped":
            raise ValueError("only a shipped order can accept returns")
        all_returns = data.get("returns", {})
        # Cancelled returns leave the active records but keep their id occupied
        # for the whole root: a return_id can never be registered again, not
        # even against a different order.
        for bucket in (all_returns, data.get("cancelled_returns", {})):
            for records in bucket.values():
                for record in records:
                    if record["return_id"] == return_id:
                        raise ValueError("return already exists: " + return_id)
        ordered = {}
        for line in order["lines"]:
            ordered[line["sku"]] = ordered.get(line["sku"], 0) + line["quantity"]
        returned = {}
        for record in all_returns.get(order_id, []):
            for line in record["lines"]:
                returned[line["sku"]] = returned.get(line["sku"], 0) + line["quantity"]
        for sku, quantity in requested.items():
            if sku not in ordered:
                raise ValueError("sku not in original order: " + sku)
            if returned.get(sku, 0) + quantity > ordered[sku]:
                raise ValueError("returned quantity exceeds ordered quantity: " + sku)
        record = {
            "order_id": order_id,
            "return_id": return_id,
            "lines": [{"sku": sku, "quantity": requested[sku]} for sku in sorted(requested)],
        }
        data.setdefault("returns", {}).setdefault(order_id, []).append(record)
        self._record_event(data, order_id, "record-return", record, False)
        self._write(data)
        return record

    def get_returns(self, order_id):
        order_id = text(order_id, "order_id")
        data = self._read()
        order = data.get("orders", {}).get(order_id)
        if order is None:
            raise ValueError("unknown order: " + order_id)
        records = sorted(data.get("returns", {}).get(order_id, []), key=lambda x: x["return_id"])
        ordered = {}
        for line in order["lines"]:
            ordered[line["sku"]] = ordered.get(line["sku"], 0) + line["quantity"]
        returned = {}
        for record in records:
            for line in record["lines"]:
                returned[line["sku"]] = returned.get(line["sku"], 0) + line["quantity"]
        remaining = [{"sku": sku, "quantity": ordered[sku] - returned.get(sku, 0)} for sku in sorted(ordered)]
        return {"order_id": order_id, "records": records, "remaining": remaining}

    def cancel_return(self, return_id):
        # Whole-registration cancellation only: it frees returnable quantity
        # (the record stops counting in remaining) but never touches stock,
        # reservations, order status, amounts, lines or shipment. The return_id
        # stays occupied for the whole root.
        return_id = text(return_id, "return_id")
        data = self._read()
        located = self._find_return(data, return_id)
        if located is None:
            raise ValueError("unknown return: " + return_id)
        order_id, record, cancelled = located
        if cancelled:
            raise ValueError("return already cancelled: " + return_id)
        order = data.get("orders", {}).get(order_id)
        if order is None or order["status"] != "shipped":
            raise ValueError("only a shipped order can cancel a return: " + order_id)
        if return_id in data.get("return_receipts", {}):
            raise ValueError("return already received: " + return_id)
        # Build the success snapshot before mutating; lines are already merged
        # by sku and sorted from record_return, but copy only sku/quantity so
        # the result never carries extra fields.
        result = {
            "order_id": order_id,
            "return_id": return_id,
            "lines": [{"sku": line["sku"], "quantity": line["quantity"]} for line in record["lines"]],
        }
        records = data["returns"][order_id]
        records.remove(record)
        if not records:
            del data["returns"][order_id]
        data.setdefault("cancelled_returns", {}).setdefault(order_id, []).append(copy.deepcopy(record))
        self._record_event(data, order_id, "cancel-return", result, False)
        self._write(data)
        return result

    def _find_return(self, data, return_id):
        # Returns (order_id, record, cancelled) for both active and cancelled
        # registrations, so callers can tell an occupied-but-cancelled id from
        # an unknown one.
        for records in data.get("returns", {}).values():
            for record in records:
                if record["return_id"] == return_id:
                    return record["order_id"], record, False
        for records in data.get("cancelled_returns", {}).values():
            for record in records:
                if record["return_id"] == return_id:
                    return record["order_id"], record, True
        return None

    def receive_return(self, return_id):
        return_id = text(return_id, "return_id")
        data = self._read()
        located = self._find_return(data, return_id)
        if located is None:
            raise ValueError("unknown return: " + return_id)
        order_id, record, cancelled = located
        if cancelled:
            raise ValueError("return already cancelled: " + return_id)
        order = data.get("orders", {}).get(order_id)
        if order is None or order["status"] != "shipped":
            raise ValueError("only a shipped order can receive a return: " + order_id)
        if return_id in data.get("return_receipts", {}):
            raise ValueError("return already received: " + return_id)
        products = data.get("products", {})
        inventory = data.get("inventory", {})
        quantities = {}
        for line in record["lines"]:
            quantities[line["sku"]] = quantities.get(line["sku"], 0) + line["quantity"]
        # Validate every product and build the snapshot before touching any
        # inventory entry, so a rejected receive leaves stock untouched.
        result_lines = []
        for sku in sorted(quantities):
            if sku not in products:
                raise ValueError("unknown product: " + sku)
            entry = inventory.get(sku)
            if entry is None:
                raise ValueError("product is not managed: " + sku)
            quantity = quantities[sku]
            before = self._stock_view(sku, entry)
            on_hand = entry["on_hand"] + quantity
            after = {"sku": sku, "on_hand": on_hand, "reserved": entry["reserved"], "available": on_hand - entry["reserved"]}
            result_lines.append({"sku": sku, "quantity": quantity, "before": before, "after": after})
        for line in result_lines:
            inventory[line["sku"]]["on_hand"] += line["quantity"]
        result = {"order_id": order_id, "return_id": return_id, "lines": result_lines}
        data.setdefault("return_receipts", {})[return_id] = copy.deepcopy(result)
        self._record_event(data, order_id, "receive-return", result, False)
        self._write(data)
        return result

    def get_return_receipt(self, return_id):
        return_id = text(return_id, "return_id")
        receipt = self._read().get("return_receipts", {}).get(return_id)
        if receipt is None:
            raise ValueError("return has not been received: " + return_id)
        return copy.deepcopy(receipt)

    def history(self, order_id):
        order_id = text(order_id, "order_id")
        data = self._read()
        order = data.get("orders", {}).get(order_id)
        if order is None:
            raise ValueError("unknown order: " + order_id)
        document = data.get("history", {}).get(order_id)
        if document is None:
            # Orders created before history existed: never fabricate events
            # from their current status or returns.
            return {"order_id": order_id, "status": order["status"], "complete": False, "events": []}
        return {
            "order_id": order_id,
            "status": order["status"],
            "complete": document["complete"],
            "events": copy.deepcopy(document["events"]),
        }
