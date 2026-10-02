import copy
from .storage import JsonStore, text, positive, calendar_date

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

    def _record_stock_event(self, data, sku, action, reference_id, before, after, complete=False):
        # Each sku counts its own stock events from 1; products managed before
        # stock history existed start with complete=False the first time a new
        # event is recorded, while the restock that first manages a product
        # starts a complete history.
        document = data.setdefault("stock_history", {}).setdefault(
            sku, {"complete": complete, "events": []}
        )
        document["events"].append({
            "sequence": len(document["events"]) + 1,
            "action": action,
            "reference_id": reference_id,
            "before": copy.deepcopy(before),
            "after": copy.deepcopy(after),
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

    def reprice_products(self, lines):
        # All-or-nothing batch reprice: every line is normalized, every sku is
        # checked against the catalog and every expected price against the
        # current price before any product is touched, so a rejected batch
        # leaves the file byte-for-byte untouched and never creates the data
        # directory. Paused or unmanaged products may be repriced without
        # resuming sales or managing stock; nothing but price_cents changes.
        if not isinstance(lines, list) or not lines:
            raise ValueError("lines must be a nonempty list")
        requested = []
        seen = set()
        for line in lines:
            if not isinstance(line, dict):
                raise ValueError("each line must be an object with sku, expected_price_cents and price_cents")
            sku = text(line.get("sku"), "sku")
            expected_price = line.get("expected_price_cents")
            target_price = line.get("price_cents")
            for value, label in ((expected_price, "expected_price_cents"), (target_price, "price_cents")):
                if type(value) is not int or value < 0:
                    raise ValueError(label + " must be a nonnegative integer")
            if sku in seen:
                raise ValueError("duplicate sku in reprice: " + sku)
            seen.add(sku)
            requested.append((sku, expected_price, target_price))
        data = self._read()
        products = data.get("products", {})
        # Verify every sku against the catalog and every expected price against
        # the current price before mutating anything.
        targets = {}
        changed = False
        for sku, expected_price, target_price in requested:
            product = products.get(sku)
            if product is None:
                raise ValueError("unknown product: " + sku)
            if expected_price != product["price_cents"]:
                raise ValueError("expected price does not match current price: " + sku)
            targets[sku] = (product, target_price)
            if target_price != product["price_cents"]:
                changed = True
        # Build views from the catalog state: before from the current state,
        # after from what the price will be once applied.
        results = []
        for sku in sorted(targets):
            product, target_price = targets[sku]
            before = self._product_view(product)
            after = dict(before)
            after["price_cents"] = target_price
            results.append({"sku": sku, "before": before, "after": after})
        if not changed:
            # Every target equals the current price: still return the full
            # result, but no file is written.
            return results
        for sku, (_, target_price) in targets.items():
            products[sku]["price_cents"] = target_price
        self._write(data)
        return results

    def restock(self, sku, quantity):
        sku = text(sku, "sku")
        quantity = positive(quantity, "quantity")
        data = self._read()
        if sku not in data.get("products", {}):
            raise ValueError("unknown product: " + sku)
        inventory = data.setdefault("inventory", {})
        entry = inventory.get(sku)
        if entry is None:
            # First successful restock manages the product; the before
            # snapshot is the unmanaged stock view and the history is complete.
            before = {"sku": sku, "on_hand": None, "reserved": 0, "available": None}
            entry = inventory[sku] = {"on_hand": 0, "reserved": 0}
            complete = True
        else:
            before = self._stock_view(sku, entry)
            complete = False
        entry["on_hand"] += quantity
        self._record_stock_event(data, sku, "restock", None, before, self._stock_view(sku, entry), complete)
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
        for line in result_lines:
            # A count that confirms the current on_hand is no stock change.
            if line["delta"]:
                self._record_stock_event(
                    data, line["sku"], "count-stock", count_id, line["before"], line["after"]
                )
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
            for sku in sorted(reservations):
                entry = inventory[sku]
                before = self._stock_view(sku, entry)
                entry["reserved"] += reservations[sku]
                self._record_stock_event(data, sku, "place", order_id, before, self._stock_view(sku, entry))
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
        # leaves stock, reservations and history untouched. Stock history sees
        # only the final net change per sku, never the temporary release and
        # re-reservation below.
        affected = sorted(sku for sku in set(own) | set(reservations) if inventory.get(sku) is not None)
        before_views = {sku: self._stock_view(sku, inventory[sku]) for sku in affected}
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
        for sku in affected:
            after = self._stock_view(sku, inventory[sku])
            if after != before_views[sku]:
                self._record_stock_event(data, sku, "amend", order_id, before_views[sku], after)
        order["lines"] = items
        order["total_cents"] = sum(x["subtotal_cents"] for x in items)
        self._record_event(data, order_id, "amend", order, False)
        self._write(data)
        return order

    def reserve_order(self, order_id):
        # Top up reservations for a placed order whose products became managed
        # after it was placed. Deal lines, prices, amounts and status are never
        # touched, and paused sales never block the top-up.
        order_id = text(order_id, "order_id")
        data = self._read()
        order = data.get("orders", {}).get(order_id)
        if order is None:
            raise ValueError("unknown order: " + order_id)
        if order["status"] != "placed":
            raise ValueError("only a placed order can be reserved")
        needed = {}
        for line in order["lines"]:
            needed[line["sku"]] = needed.get(line["sku"], 0) + line["quantity"]
        products = data.get("products", {})
        inventory = data.get("inventory", {})
        own = data.get("reservations", {}).get(order_id, {})
        # Validate every sku and compute every addition against the current
        # availability before touching any inventory entry, so a rejected
        # top-up leaves stock, reservations and history untouched. A missing
        # reservation record reads as zero.
        additions = {}
        for sku in sorted(needed):
            if sku not in products:
                raise ValueError("unknown product: " + sku)
            entry = inventory.get(sku)
            if entry is None:
                # Unmanaged products stay unlimited and are never auto-managed.
                continue
            added = max(0, needed[sku] - own.get(sku, 0))
            if added > entry["on_hand"] - entry["reserved"]:
                raise ValueError("insufficient stock: " + sku)
            if added:
                additions[sku] = added
        lines = []
        for sku in sorted(needed):
            if inventory.get(sku) is None:
                lines.append({"sku": sku, "quantity": needed[sku], "added": 0, "reserved": 0})
            else:
                added = additions.get(sku, 0)
                lines.append({
                    "sku": sku,
                    "quantity": needed[sku],
                    "added": added,
                    "reserved": own.get(sku, 0) + added,
                })
        result = {"order_id": order_id, "lines": lines}
        if not additions:
            # Nothing to add: the result is still returned, but no file is
            # written and no events are appended.
            return result
        for sku in sorted(additions):
            entry = inventory[sku]
            before = self._stock_view(sku, entry)
            entry["reserved"] += additions[sku]
            self._record_stock_event(data, sku, "reserve-order", order_id, before, self._stock_view(sku, entry))
        reservations = data.setdefault("reservations", {}).setdefault(order_id, {})
        for sku, added in additions.items():
            reservations[sku] = reservations.get(sku, 0) + added
        self._record_event(data, order_id, "reserve-order", result, False)
        self._write(data)
        return result

    def reserve_batch(self, order_ids):
        # Priority batch top-up: orders are processed in the given order
        # (input order is the priority); each order either gets its full new
        # demand or is skipped whole and consumes nothing, and later orders
        # keep being processed. The whole request is validated before any
        # mutation, so a rejected batch leaves data, history and sequences
        # untouched and never creates the data directory.
        if not isinstance(order_ids, list) or not order_ids:
            raise ValueError("order_ids must be a nonempty list")
        selected = []
        seen = set()
        for order_id in order_ids:
            order_id = text(order_id, "order_id")
            if order_id in seen:
                raise ValueError("duplicate order_id: " + order_id)
            seen.add(order_id)
            selected.append(order_id)
        data = self._read()
        orders = data.get("orders", {})
        products = data.get("products", {})
        inventory = data.get("inventory", {})
        all_reservations = data.get("reservations", {})
        # Merge duplicate sku lines inside each order first; the whole request
        # is rejected if any selected order is missing, no longer placed, or
        # carries a product outside the current catalog.
        demanded = {}  # order_id -> {sku: merged quantity}
        for order_id in selected:
            order = orders.get(order_id)
            if order is None:
                raise ValueError("unknown order: " + order_id)
            if order["status"] != "placed":
                raise ValueError("only a placed order can be reserved: " + order_id)
            per_order = {}
            for line in order["lines"]:
                per_order[line["sku"]] = per_order.get(line["sku"], 0) + line["quantity"]
            for sku in per_order:
                if sku not in products:
                    raise ValueError("unknown product: " + sku)
            demanded[order_id] = per_order
        # Plan against the shared margin (available after every existing
        # reservation, including orders outside the batch) without touching
        # data: a fitting order consumes margin, a skipped one consumes
        # nothing. Existing reservations are never released or transferred.
        remaining = {sku: entry["on_hand"] - entry["reserved"] for sku, entry in inventory.items()}
        plans = []  # (order_id, fits, needs, additions, available at turn)
        for order_id in selected:
            per_order = demanded[order_id]
            own = all_reservations.get(order_id, {})
            needs = {sku: max(0, qty - own.get(sku, 0)) for sku, qty in per_order.items()}
            at_turn = {sku: remaining.get(sku, 0) for sku in per_order if sku in inventory}
            fits = all(needs[sku] <= at_turn[sku] for sku in at_turn)
            additions = {}
            if fits:
                for sku, need in needs.items():
                    if sku in inventory and need:
                        additions[sku] = need
                        remaining[sku] -= need
            plans.append((order_id, fits, needs, additions, at_turn))
        results = []
        for order_id, fits, needs, additions, at_turn in plans:
            per_order = demanded[order_id]
            own = all_reservations.get(order_id, {})
            lines = []
            for sku in sorted(per_order):
                if sku not in inventory:
                    # Unmanaged products stay unlimited and are never
                    # auto-managed: added, reserved and shortfall read as zero.
                    lines.append({"sku": sku, "quantity": per_order[sku],
                                  "added": 0, "reserved": 0, "shortfall": 0})
                    continue
                added = additions.get(sku, 0)
                shortfall = 0 if fits else max(0, needs[sku] - at_turn[sku])
                lines.append({
                    "sku": sku,
                    "quantity": per_order[sku],
                    "added": added,
                    "reserved": own.get(sku, 0) + added,
                    "shortfall": shortfall,
                })
            results.append({"order_id": order_id, "can_reserve": fits, "lines": lines})
        if not any(additions for _, _, _, additions, _ in plans):
            # Nothing to add anywhere: the result is still returned, but no
            # file is written and no events are appended.
            return results
        # Apply in request order so shared-product stock snapshots chain.
        for (order_id, fits, needs, additions, at_turn), result in zip(plans, results):
            if not additions:
                continue
            for sku in sorted(additions):
                entry = inventory[sku]
                before = self._stock_view(sku, entry)
                entry["reserved"] += additions[sku]
                self._record_stock_event(data, sku, "reserve-order", order_id, before, self._stock_view(sku, entry))
            reservations = data.setdefault("reservations", {}).setdefault(order_id, {})
            for sku, added in additions.items():
                reservations[sku] = reservations.get(sku, 0) + added
            # The order event keeps the single reserve-order result structure.
            event_lines = [
                {"sku": line["sku"], "quantity": line["quantity"],
                 "added": line["added"], "reserved": line["reserved"]}
                for line in result["lines"]
            ]
            self._record_event(data, order_id, "reserve-order",
                               {"order_id": order_id, "lines": event_lines}, False)
        self._write(data)
        return results

    def transfer_reservation(self, source_id, target_id, lines):
        # Move reservations between two placed orders: the source gives up
        # quantities it actually holds and the target receives them, never
        # touching on_hand, total reserved, availability, other orders, status,
        # deal lines, prices or amounts. Paused sales never block a transfer.
        source_id = text(source_id, "source_id")
        target_id = text(target_id, "target_id")
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
        orders = data.get("orders", {})
        source = orders.get(source_id)
        if source is None:
            raise ValueError("unknown order: " + source_id)
        target = orders.get(target_id)
        if target is None:
            raise ValueError("unknown order: " + target_id)
        if source["status"] != "placed":
            raise ValueError("only a placed order can transfer a reservation: " + source_id)
        if target["status"] != "placed":
            raise ValueError("only a placed order can receive a reservation: " + target_id)
        if source_id == target_id:
            raise ValueError("source and target must be different orders")
        products = data.get("products", {})
        inventory = data.get("inventory", {})
        source_ordered = {}
        for line in source["lines"]:
            source_ordered[line["sku"]] = source_ordered.get(line["sku"], 0) + line["quantity"]
        target_ordered = {}
        for line in target["lines"]:
            target_ordered[line["sku"]] = target_ordered.get(line["sku"], 0) + line["quantity"]
        all_reservations = data.get("reservations", {})
        source_held = all_reservations.get(source_id, {})
        target_held = all_reservations.get(target_id, {})
        # Validate every sku against catalog, managed stock, both orders' lines
        # and both current balances before touching any record, so a rejected
        # transfer leaves reservations and history untouched.
        for sku in sorted(requested):
            if sku not in products:
                raise ValueError("unknown product: " + sku)
            if sku not in inventory:
                raise ValueError("product is not managed: " + sku)
            if sku not in source_ordered or sku not in target_ordered:
                raise ValueError("sku must appear in both orders: " + sku)
        for sku, quantity in requested.items():
            if quantity > source_held.get(sku, 0):
                raise ValueError("source reservation is insufficient: " + sku)
            if target_held.get(sku, 0) + quantity > target_ordered[sku]:
                raise ValueError("target reservation would exceed ordered quantity: " + sku)
        # Source decrement and target increment happen together; the total
        # reserved per sku is unchanged, so no stock history is recorded.
        records = data.setdefault("reservations", {})
        source_record = records.setdefault(source_id, {})
        target_record = records.setdefault(target_id, {})
        result_lines = []
        for sku in sorted(requested):
            quantity = requested[sku]
            source_after = source_record.get(sku, 0) - quantity
            target_after = target_record.get(sku, 0) + quantity
            if source_after:
                source_record[sku] = source_after
            else:
                source_record.pop(sku, None)
            target_record[sku] = target_after
            result_lines.append({
                "sku": sku,
                "quantity": quantity,
                "source_reserved": source_after,
                "target_reserved": target_after,
            })
        if not source_record:
            records.pop(source_id, None)
        result = {"source_id": source_id, "target_id": target_id, "lines": result_lines}
        # Both orders keep the same full snapshot; each continues its own
        # sequence and legacy orders start at 1 with complete=False.
        self._record_event(data, source_id, "transfer-reservation", result, False)
        self._record_event(data, target_id, "transfer-reservation", result, False)
        self._write(data)
        return result

    def release_reservation(self, order_id, lines):
        # Release part of a placed order's own reservations back to available
        # stock without cancelling the order or naming a receiving order: this
        # order's reservation and the product total drop together, availability
        # rises by the same amount, and on_hand, deal lines, prices, amounts,
        # status and other orders' reservations are never touched. Paused sales
        # never block a release.
        order_id = text(order_id, "order_id")
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
        if order["status"] != "placed":
            raise ValueError("only a placed order can release a reservation")
        products = data.get("products", {})
        inventory = data.get("inventory", {})
        ordered = set()
        for line in order["lines"]:
            ordered.add(line["sku"])
        own = data.get("reservations", {}).get(order_id, {})
        # Validate every sku against catalog, managed stock, the order's current
        # lines and its current balance before touching any record, so a
        # rejected release leaves stock, reservations and history untouched. A
        # missing reservation record reads as zero.
        for sku in sorted(requested):
            if sku not in products:
                raise ValueError("unknown product: " + sku)
            if sku not in inventory:
                raise ValueError("product is not managed: " + sku)
            if sku not in ordered:
                raise ValueError("sku not in order: " + sku)
        for sku, quantity in requested.items():
            if quantity > own.get(sku, 0):
                raise ValueError("order reservation is insufficient: " + sku)
        records = data.setdefault("reservations", {})
        record = records.setdefault(order_id, {})
        result_lines = []
        for sku in sorted(requested):
            quantity = requested[sku]
            entry = inventory[sku]
            before = self._stock_view(sku, entry)
            entry["reserved"] -= quantity
            after = self._stock_view(sku, entry)
            remaining = record.get(sku, 0) - quantity
            if remaining:
                record[sku] = remaining
            else:
                record.pop(sku, None)
            self._record_stock_event(data, sku, "release-reservation", order_id, before, after)
            result_lines.append({
                "sku": sku,
                "quantity": quantity,
                "reserved": remaining,
                "before": before,
                "after": after,
            })
        if not record:
            records.pop(order_id, None)
        result = {"order_id": order_id, "lines": result_lines}
        self._record_event(data, order_id, "release-reservation", result, False)
        self._write(data)
        return result

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
            for sku in sorted(reservations):
                entry = inventory.get(sku)
                if entry is not None:
                    before = self._stock_view(sku, entry)
                    entry["reserved"] -= reservations[sku]
                    self._record_stock_event(data, sku, "cancel", order_id, before, self._stock_view(sku, entry))
        order["status"] = "cancelled"
        self._record_event(data, order_id, "cancel", order, False)
        self._write(data)
        return order

    def _ship_order(self, data, order, carrier, tracking_no):
        # Shared mutation for ship and ship_batch: the caller has already
        # validated that the order is placed. Deducts only this order's actual
        # reservations, marks it shipped and appends the ship event; the caller
        # is responsible for writing `data`.
        order_id = order["order_id"]
        reservations = data.get("reservations", {}).pop(order_id, None)
        if reservations:
            inventory = data.get("inventory", {})
            for sku in sorted(reservations):
                entry = inventory.get(sku)
                if entry is not None:
                    before = self._stock_view(sku, entry)
                    entry["on_hand"] -= reservations[sku]
                    entry["reserved"] -= reservations[sku]
                    self._record_stock_event(data, sku, "ship", order_id, before, self._stock_view(sku, entry))
        order["status"] = "shipped"
        order["shipment"] = {"carrier": carrier, "tracking_no": tracking_no}
        self._record_event(data, order_id, "ship", order, False)

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
        self._ship_order(data, order, carrier, tracking_no)
        self._write(data)
        return order

    def ship_batch(self, shipments):
        # All-or-nothing batch shipment: every entry is normalized and every
        # referenced order is checked before any mutation, so a rejected batch
        # leaves orders, stock, reservations and history byte-for-byte untouched
        # and never creates the data directory.
        if not isinstance(shipments, list) or not shipments:
            raise ValueError("shipments must be a nonempty list")
        entries = []
        seen = set()
        for item in shipments:
            if not isinstance(item, dict):
                raise ValueError("each shipment must be an object with order_id, carrier and tracking_no")
            order_id = text(item.get("order_id"), "order_id")
            carrier = text(item.get("carrier"), "carrier")
            tracking_no = text(item.get("tracking_no"), "tracking_no")
            if order_id in seen:
                raise ValueError("duplicate order_id: " + order_id)
            seen.add(order_id)
            entries.append((order_id, carrier, tracking_no))
        data = self._read()
        orders = data.get("orders", {})
        planned = []
        for order_id, carrier, tracking_no in entries:
            order = orders.get(order_id)
            if order is None:
                raise ValueError("unknown order: " + order_id)
            if order["status"] != "placed":
                raise ValueError("only a placed order can be shipped: " + order_id)
            planned.append((order, carrier, tracking_no))
        for order, carrier, tracking_no in planned:
            self._ship_order(data, order, carrier, tracking_no)
        self._write(data)
        return sorted((order for order, _, _ in planned), key=lambda x: x["order_id"])

    def confirm_delivery(self, order_id, recipient, delivered_on):
        # Offline sign-off: a shipped order becomes delivered using a date the
        # caller provides (the system clock is never read). It never touches
        # lines, amounts, shipment, stock, reservations, existing returns or
        # stock history; only a recipient/delivered_on delivery object is added
        # and one confirm-delivery event is appended.
        order_id = text(order_id, "order_id")
        recipient = text(recipient, "recipient")
        delivered_on = calendar_date(delivered_on, "delivered_on")
        data = self._read()
        order = data.get("orders", {}).get(order_id)
        if order is None:
            raise ValueError("unknown order: " + order_id)
        if order["status"] == "delivered":
            raise ValueError("order already delivered")
        if order["status"] != "shipped":
            raise ValueError("only a shipped order can be delivered")
        order["status"] = "delivered"
        order["delivery"] = {"recipient": recipient, "delivered_on": delivered_on}
        self._record_event(data, order_id, "confirm-delivery", order, False)
        self._write(data)
        return order

    def pick_list(self, order_ids):
        # Read-only picking summary across the selected placed orders: it merges
        # quantities by sku while keeping per-order demand and reservation
        # detail. It never writes, never fabricates reservations for orders
        # placed before a product became managed, and never counts reservations
        # held by orders outside the selection.
        if not isinstance(order_ids, list) or not order_ids:
            raise ValueError("order_ids must be a nonempty list")
        selected = []
        seen = set()
        for order_id in order_ids:
            order_id = text(order_id, "order_id")
            if order_id in seen:
                raise ValueError("duplicate order_id: " + order_id)
            seen.add(order_id)
            selected.append(order_id)
        data = self._read()
        orders = data.get("orders", {})
        all_reservations = data.get("reservations", {})
        inventory = data.get("inventory", {})
        # Merge duplicate sku lines inside each order first; the whole query is
        # rejected if any selected order is missing or no longer placed.
        demanded = {}  # order_id -> {sku: merged quantity}
        for order_id in selected:
            order = orders.get(order_id)
            if order is None:
                raise ValueError("unknown order: " + order_id)
            if order["status"] != "placed":
                raise ValueError("only a placed order can be picked: " + order_id)
            per_order = {}
            for line in order["lines"]:
                per_order[line["sku"]] = per_order.get(line["sku"], 0) + line["quantity"]
            demanded[order_id] = per_order
        skus = set()
        for per_order in demanded.values():
            skus.update(per_order)
        lines = []
        for sku in sorted(skus):
            quantity = 0
            reserved = 0
            order_rows = []
            for order_id in sorted(demanded):
                per_order = demanded[order_id]
                if sku not in per_order:
                    continue
                order_quantity = per_order[sku]
                quantity += order_quantity
                order_reserved = all_reservations.get(order_id, {}).get(sku, 0)
                reserved += order_reserved
                order_rows.append({
                    "order_id": order_id,
                    "quantity": order_quantity,
                    "reserved": order_reserved,
                })
            entry = inventory.get(sku)
            if entry is None:
                # Legacy data without an inventory record stays unmanaged: no
                # availability, no reservations and no shortfall.
                available = None
                reserved = 0
                shortfall = 0
                for row in order_rows:
                    row["reserved"] = 0
            else:
                available = entry["on_hand"] - entry["reserved"]
                shortfall = max(0, quantity - reserved - available)
            lines.append({
                "sku": sku,
                "quantity": quantity,
                "reserved": reserved,
                "available": available,
                "shortfall": shortfall,
                "orders": order_rows,
            })
        return {"order_ids": sorted(selected), "lines": lines}

    def reservation_plan(self, order_ids):
        # Read-only priority preview of reservation top-ups: orders are previewed
        # in the given order; an order whose new demand fits the remaining margin
        # consumes that margin, a short order consumes nothing and later orders
        # keep being evaluated. Existing reservations are never released or
        # transferred; this never writes.
        if not isinstance(order_ids, list) or not order_ids:
            raise ValueError("order_ids must be a nonempty list")
        selected = []
        seen = set()
        for order_id in order_ids:
            order_id = text(order_id, "order_id")
            if order_id in seen:
                raise ValueError("duplicate order_id: " + order_id)
            seen.add(order_id)
            selected.append(order_id)
        data = self._read()
        orders = data.get("orders", {})
        products = data.get("products", {})
        inventory = data.get("inventory", {})
        all_reservations = data.get("reservations", {})
        # Merge duplicate sku lines inside each order first; the whole query is
        # rejected if any selected order is missing, no longer placed, or carries
        # a product outside the current catalog.
        demanded = {}  # order_id -> {sku: merged quantity}
        for order_id in selected:
            order = orders.get(order_id)
            if order is None:
                raise ValueError("unknown order: " + order_id)
            if order["status"] != "placed":
                raise ValueError("only a placed order can be reserved: " + order_id)
            per_order = {}
            for line in order["lines"]:
                per_order[line["sku"]] = per_order.get(line["sku"], 0) + line["quantity"]
            for sku in per_order:
                if sku not in products:
                    raise ValueError("unknown product: " + sku)
            demanded[order_id] = per_order
        # The margin starts at stock's current available per managed sku and is
        # only reduced by earlier orders that could be completed.
        remaining = {}
        for sku, entry in inventory.items():
            remaining[sku] = entry["on_hand"] - entry["reserved"]
        results = []
        for order_id in selected:
            per_order = demanded[order_id]
            own = all_reservations.get(order_id, {})
            needs = {sku: max(0, qty - own.get(sku, 0)) for sku, qty in per_order.items()}
            # Evaluate every managed sku before consuming anything: an order
            # either covers all of its new demand or none of it.
            fits = True
            for sku in sorted(per_order):
                if sku in inventory and needs[sku] > remaining.get(sku, 0):
                    fits = False
                    break
            lines = []
            for sku in sorted(per_order):
                quantity = per_order[sku]
                reserved = own.get(sku, 0)
                demand = needs[sku]
                if sku not in inventory:
                    # Unmanaged products (including legacy data without inventory
                    # records) never constrain demand and never consume margin.
                    lines.append({
                        "sku": sku,
                        "quantity": quantity,
                        "reserved": 0,
                        "available": None,
                        "shortfall": 0,
                    })
                    continue
                available = remaining.get(sku, 0)
                if fits:
                    shortfall = 0
                else:
                    shortfall = max(0, demand - available)
                lines.append({
                    "sku": sku,
                    "quantity": quantity,
                    "reserved": reserved,
                    "available": available,
                    "shortfall": shortfall,
                })
            if fits:
                for sku in per_order:
                    if sku in inventory:
                        remaining[sku] = remaining.get(sku, 0) - needs[sku]
            results.append({
                "order_id": order_id,
                "can_reserve": fits,
                "lines": lines,
            })
        return results

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
        if order["status"] not in ("shipped", "delivered"):
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
        if order is None or order["status"] not in ("shipped", "delivered"):
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
        if order is None or order["status"] not in ("shipped", "delivered"):
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
            self._record_stock_event(
                data, line["sku"], "receive-return", return_id, line["before"], line["after"]
            )
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

    def return_worklist(self, stage="pending", order_id=None):
        # Read-only cross-order worklist: active registrations read as pending
        # unless they hold a receipt (received), cancelled registrations read as
        # cancelled but stay visible. It never writes, never fabricates
        # registrations from history, and treats missing receipts or cancellation
        # buckets simply as absent.
        if not isinstance(stage, str):
            raise ValueError("stage must be one of: pending, received, cancelled, all")
        stage = stage.strip()
        if stage not in ("pending", "received", "cancelled", "all"):
            raise ValueError("stage must be one of: pending, received, cancelled, all")
        if order_id is not None:
            order_id = text(order_id, "order_id")
        data = self._read()
        orders = data.get("orders", {})
        if order_id is not None and order_id not in orders:
            raise ValueError("unknown order: " + order_id)
        receipts = data.get("return_receipts", {})
        # Collect (record, record_stage) pairs in scope. Active records come
        # first, cancelled ones after; the final result is sorted by return_id.
        scoped = []
        for bucket, is_cancelled in (
            (data.get("returns", {}), False),
            (data.get("cancelled_returns", {}), True),
        ):
            keys = (order_id,) if order_id is not None else sorted(bucket)
            for key in keys:
                for record in bucket.get(key, ()):
                    if is_cancelled:
                        record_stage = "cancelled"
                    elif record["return_id"] in receipts:
                        record_stage = "received"
                    else:
                        record_stage = "pending"
                    if stage == "all" or record_stage == stage:
                        scoped.append((record, record_stage))
        # Every registration the worklist would actually contain must still
        # belong to an existing shipped order; validate before building any view
        # so one bad legacy record rejects the whole query instead of surfacing
        # partial results.
        for record, _ in scoped:
            order = orders.get(record["order_id"])
            if order is None:
                raise ValueError("unknown order: " + record["order_id"])
            if order["status"] not in ("shipped", "delivered"):
                raise ValueError("only a shipped order can be on the return worklist: " + record["order_id"])
        products = data.get("products", {})
        inventory = data.get("inventory", {})
        entries = []
        for record, record_stage in scoped:
            quantities = {}
            for line in record["lines"]:
                quantities[line["sku"]] = quantities.get(line["sku"], 0) + line["quantity"]
            lines = [{"sku": sku, "quantity": quantities[sku]} for sku in sorted(quantities)]
            blockers = []
            if record_stage == "pending":
                # Only pending entries are checked against the current catalog
                # and inventory; paused sales never block receiving.
                for sku in sorted(quantities):
                    if sku not in products:
                        blockers.append({"sku": sku, "reason": "unknown-product"})
                    elif sku not in inventory:
                        blockers.append({"sku": sku, "reason": "unmanaged"})
            entries.append({
                "order_id": record["order_id"],
                "return_id": record["return_id"],
                "stage": record_stage,
                "lines": lines,
                "can_receive": record_stage == "pending" and not blockers,
                "blockers": blockers,
            })
        entries.sort(key=lambda item: item["return_id"])
        return entries

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

    def stock_history(self, sku):
        sku = text(sku, "sku")
        data = self._read()
        if sku not in data.get("products", {}):
            raise ValueError("unknown product: " + sku)
        document = data.get("stock_history", {}).get(sku)
        if document is None:
            # A product with no stock events is either still unmanaged (its
            # empty history is complete) or was managed before stock history
            # existed (its past changes are unrecoverable and never refilled).
            managed = sku in data.get("inventory", {})
            return {"sku": sku, "complete": not managed, "events": []}
        return {
            "sku": sku,
            "complete": document["complete"],
            "events": copy.deepcopy(document["events"]),
        }
