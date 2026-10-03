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

    def reverse_stock_count(self, count_id):
        # Whole-count reversal: every line of the original count is offset by
        # the negated delta against the CURRENT on_hand, so business changes
        # after the count are preserved. Reservations are never touched. A
        # paused product may still be reversed, but a missing catalog entry,
        # an unmanaged product, a negative new on_hand or one below the
        # current reserved quantity rejects the whole reversal before any
        # inventory entry is touched.
        count_id = text(count_id, "count_id")
        data = self._read()
        record = data.get("stock_counts", {}).get(count_id)
        if record is None:
            raise ValueError("unknown stock count: " + count_id)
        if count_id in data.get("stock_count_reversals", {}):
            raise ValueError("stock count already reversed: " + count_id)
        products = data.get("products", {})
        inventory = data.get("inventory", {})
        # Validate every line and build the receipt before mutating anything,
        # so a rejected reversal leaves stock, history and sequences untouched.
        result_lines = []
        for line in sorted(record["lines"], key=lambda x: x["sku"]):
            sku = line["sku"]
            if sku not in products:
                raise ValueError("unknown product: " + sku)
            entry = inventory.get(sku)
            if entry is None:
                raise ValueError("product is not managed: " + sku)
            delta = -line["delta"]
            on_hand = entry["on_hand"] + delta
            if on_hand < 0:
                raise ValueError("reversed on_hand cannot be negative: " + sku)
            reserved = entry["reserved"]
            if on_hand < reserved:
                raise ValueError("reversed on_hand cannot be below reserved quantity: " + sku)
            result_lines.append({
                "sku": sku,
                "delta": delta,
                "before": self._stock_view(sku, entry),
                "after": {"sku": sku, "on_hand": on_hand, "reserved": reserved, "available": on_hand - reserved},
            })
        for line in result_lines:
            inventory[line["sku"]]["on_hand"] = line["after"]["on_hand"]
            # A zero-delta line confirms the current on_hand: no stock change.
            if line["delta"]:
                self._record_stock_event(
                    data, line["sku"], "reverse-stock-count", count_id, line["before"], line["after"]
                )
        result = {"count_id": count_id, "lines": result_lines}
        data.setdefault("stock_count_reversals", {})[count_id] = copy.deepcopy(result)
        self._write(data)
        return result

    def get_stock_count_reversal(self, count_id):
        count_id = text(count_id, "count_id")
        data = self._read()
        if count_id not in data.get("stock_counts", {}):
            raise ValueError("unknown stock count: " + count_id)
        record = data.get("stock_count_reversals", {}).get(count_id)
        if record is None:
            raise ValueError("stock count has not been reversed: " + count_id)
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
        # Shared by place, single-cart checkout and batch checkout: validates
        # against the current catalog and inventory inside `data`, reserves
        # stock and records the place event. Caller writes `data` once the whole
        # request has succeeded. In a batch the caller checks every cart and the
        # combined managed demand before calling this, so sequential calls on
        # the same document accumulate reservations and chain stock snapshots.
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

    def checkout_cart_part(self, cart_id, order_id, lines):
        # Partial checkout: only the selected quantities become a new order and
        # the rest of the cart survives. The merged selection is checked
        # against the cart's current quantities first, then against the current
        # catalog and stock via _place; skus left behind -- paused, out of stock
        # or missing from the catalog -- never block this checkout and keep
        # their quantities. Everything happens on one in-memory document with a
        # single write, so any failure leaves the file, sequences and the cart
        # untouched.
        cart_id = text(cart_id, "cart_id")
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
        cart = data.get("carts", {}).get(cart_id)
        if cart is None:
            raise ValueError("unknown cart: " + cart_id)
        current = {}
        for line in cart["lines"]:
            current[line["sku"]] = current.get(line["sku"], 0) + line["quantity"]
        for sku, quantity in requested.items():
            if sku not in current:
                raise ValueError("sku not in cart: " + sku)
            if quantity > current[sku]:
                raise ValueError("selected quantity exceeds cart quantity: " + sku)
        # _place prices and reserves against the current catalog and stock and
        # records the place events; it validates everything before mutating.
        order = self._place(
            data, order_id,
            [{"sku": sku, "quantity": requested[sku]} for sku in sorted(requested)],
        )
        remaining = []
        for sku in sorted(current):
            quantity = current[sku] - requested.get(sku, 0)
            if quantity:
                # Zero-quantity rows are removed rather than stored.
                remaining.append({"sku": sku, "quantity": quantity})
        carts = data.get("carts", {})
        if remaining:
            cart_view = {"cart_id": cart_id, "lines": remaining}
            carts[cart_id] = cart_view
        else:
            # Selecting the whole cart settles it completely.
            carts.pop(cart_id, None)
            cart_view = None
        self._write(data)
        return {"order": order, "cart": cart_view}

    def checkout_cart_batch(self, checkouts):
        # All-or-nothing batch checkout: every entry is normalized, every cart
        # and target order id is checked, and every cart is priced against the
        # current catalog before any order is created. Managed stock uses the
        # margin left after every existing reservation; the combined demand of
        # all carts for one sku must fit it, so a shortfall anywhere (including
        # the last entry) rejects the whole request: carts, orders, inventory,
        # history and sequences stay byte-for-byte untouched and the data
        # directory is never created.
        if not isinstance(checkouts, list) or not checkouts:
            raise ValueError("checkouts must be a nonempty list")
        entries = []
        seen_carts = set()
        seen_orders = set()
        for item in checkouts:
            if not isinstance(item, dict):
                raise ValueError("each checkout must be an object with cart_id and order_id")
            cart_id = text(item.get("cart_id"), "cart_id")
            order_id = text(item.get("order_id"), "order_id")
            if cart_id in seen_carts:
                raise ValueError("duplicate cart_id: " + cart_id)
            seen_carts.add(cart_id)
            if order_id in seen_orders:
                raise ValueError("duplicate order_id: " + order_id)
            seen_orders.add(order_id)
            entries.append((cart_id, order_id))
        data = self._read()
        carts = data.get("carts", {})
        orders = data.get("orders", {})
        products = data.get("products", {})
        # Resolve every cart and target id and price every line against the
        # current catalog before any mutation; merge the combined demand so a
        # shared product is checked against the batch total.
        planned = []
        combined = {}
        for cart_id, order_id in entries:
            cart = carts.get(cart_id)
            if cart is None:
                raise ValueError("unknown cart: " + cart_id)
            if order_id in orders:
                raise ValueError("order already exists")
            needed = {}
            for line in cart["lines"]:
                sku = line["sku"]
                product = products.get(sku)
                if product is None:
                    raise ValueError("unknown product: " + sku)
                if not self._is_enabled(product):
                    raise ValueError("product is not available for sale: " + sku)
                needed[sku] = needed.get(sku, 0) + line["quantity"]
            planned.append((cart_id, order_id, needed))
            for sku, quantity in needed.items():
                combined[sku] = combined.get(sku, 0) + quantity
        inventory = data.get("inventory", {})
        # Unmanaged products never constrain the batch; managed demand must fit
        # the availability left after every existing reservation.
        for sku in sorted(combined):
            entry = inventory.get(sku)
            if entry is not None and combined[sku] > entry["on_hand"] - entry["reserved"]:
                raise ValueError("insufficient stock: " + sku)
        # All checks passed: place in request order on the same in-memory
        # document. Each _place reserves against the reservations earlier
        # checkouts in this batch just added, so shared-product stock snapshots
        # chain; the combined check above guarantees none of them can fail.
        created = []
        for cart_id, order_id, needed in planned:
            order = self._place(
                data, order_id,
                [{"sku": sku, "quantity": needed[sku]} for sku in sorted(needed)],
            )
            created.append(order)
            carts.pop(cart_id, None)
        self._write(data)
        return sorted(created, key=lambda order: order["order_id"])

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

    def cancel_batch(self, order_ids):
        # All-or-nothing batch cancel: every id is normalized and every order
        # is checked before any mutation, so a rejected batch (even one whose
        # last entry is invalid) leaves orders, stock, reservations, history
        # and sequences byte-for-byte untouched and never creates the data
        # directory.
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
        planned = []
        for order_id in selected:
            order = orders.get(order_id)
            if order is None:
                raise ValueError("unknown order: " + order_id)
            if order["status"] != "placed":
                raise ValueError("only a placed order can be cancelled: " + order_id)
            planned.append(order)
        # Apply in request order so shared-product stock snapshots chain; the
        # returned array is sorted by order id without changing event order.
        # Only each order's actual reservations are released -- never
        # quantities inferred from its lines -- and unmanaged or
        # later-managed products and legacy orders without reservation
        # records release nothing.
        all_reservations = data.get("reservations", {})
        inventory = data.get("inventory", {})
        for order in planned:
            order_id = order["order_id"]
            reservations = all_reservations.pop(order_id, None)
            if reservations:
                for sku in sorted(reservations):
                    entry = inventory.get(sku)
                    if entry is not None:
                        before = self._stock_view(sku, entry)
                        entry["reserved"] -= reservations[sku]
                        self._record_stock_event(data, sku, "cancel", order_id, before, self._stock_view(sku, entry))
            order["status"] = "cancelled"
            self._record_event(data, order_id, "cancel", order, False)
        self._write(data)
        return sorted(planned, key=lambda order: order["order_id"])

    def reopen_order(self, order_id):
        # Reopen a cancelled order under its original id: the deal (line order
        # and duplicate lines, quantities, deal prices, subtotals and total) is
        # preserved exactly and never repriced against the current catalog,
        # while every product is re-validated against the current catalog and
        # stock. Managed products reserve their full merged demand against the
        # availability left after every existing reservation -- never borrowing
        # other orders' reservations or any pre-cancel allowance; unmanaged
        # products stay unlimited, are never auto-managed and hold no
        # reservation. Everything is validated before any mutation, so a
        # rejected reopen leaves the file, reservations, history and sequences
        # untouched and never creates the data directory.
        order_id = text(order_id, "order_id")
        data = self._read()
        order = data.get("orders", {}).get(order_id)
        if order is None:
            raise ValueError("unknown order: " + order_id)
        if order["status"] != "cancelled":
            raise ValueError("only a cancelled order can be reopened")
        needed = {}
        for line in order["lines"]:
            needed[line["sku"]] = needed.get(line["sku"], 0) + line["quantity"]
        products = data.get("products", {})
        inventory = data.get("inventory", {})
        # Validate every sku against the current catalog and every managed
        # demand against the current availability before touching anything.
        for sku in sorted(needed):
            product = products.get(sku)
            if product is None:
                raise ValueError("unknown product: " + sku)
            if not self._is_enabled(product):
                raise ValueError("product is not available for sale: " + sku)
            entry = inventory.get(sku)
            if entry is not None and needed[sku] > entry["on_hand"] - entry["reserved"]:
                raise ValueError("insufficient stock: " + sku)
        reservations = {sku: needed[sku] for sku in needed if sku in inventory}
        if reservations:
            for sku in sorted(reservations):
                entry = inventory[sku]
                before = self._stock_view(sku, entry)
                entry["reserved"] += reservations[sku]
                self._record_stock_event(data, sku, "reopen-order", order_id, before, self._stock_view(sku, entry))
            data.setdefault("reservations", {})[order_id] = reservations
        order["status"] = "placed"
        self._record_event(data, order_id, "reopen-order", order, False)
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

    @staticmethod
    def _shipment_info(value, label):
        # Normalizes a shipment object: it must be an object whose carrier and
        # tracking_no are nonempty strings after trimming; extra fields are
        # ignored. Used for both the supplied info and the stored shipment.
        if not isinstance(value, dict):
            raise ValueError(label + " must be an object with carrier and tracking_no")
        carrier = text(value.get("carrier"), "carrier")
        tracking_no = text(value.get("tracking_no"), "tracking_no")
        return {"carrier": carrier, "tracking_no": tracking_no}

    def correct_shipment(self, order_id, expected_shipment, shipment):
        # Correct a recorded shipment after checking the original info: only
        # shipped or delivered orders qualify, existing returns never block it,
        # and both carrier and tracking_no must match the current shipment
        # before anything is replaced. Either field may change on its own;
        # tracking numbers are never checked for uniqueness across orders.
        order_id = text(order_id, "order_id")
        expected = self._shipment_info(expected_shipment, "expected_shipment")
        target = self._shipment_info(shipment, "shipment")
        data = self._read()
        order = data.get("orders", {}).get(order_id)
        if order is None:
            raise ValueError("unknown order: " + order_id)
        if order["status"] not in ("shipped", "delivered"):
            raise ValueError("only a shipped or delivered order can have its shipment corrected")
        # A missing or textually invalid stored shipment cannot be matched and
        # is never silently overwritten.
        current = self._shipment_info(order.get("shipment"), "shipment")
        if expected != current:
            raise ValueError("expected shipment does not match current shipment")
        result = {"order_id": order_id, "before": dict(current), "after": dict(target)}
        if target == current:
            # Original info matches but the target changes nothing: still
            # return the result, but no file is written and no event is
            # appended.
            return result
        order["shipment"] = {"carrier": target["carrier"], "tracking_no": target["tracking_no"]}
        self._record_event(data, order_id, "correct-shipment", result, False)
        self._write(data)
        return result

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

    def confirm_shipment_delivery(self, carrier, tracking_no, recipient, delivered_on):
        # Bulk offline sign-off: every order currently shipped under one
        # carrier + tracking number combination becomes delivered at once,
        # using a caller-provided date (the system clock is never read). The
        # matching rule is shipment_orders': only the order's current shipment
        # object counts, never historical ship/correct-shipment snapshots, and
        # only shipped or delivered orders qualify. A matched order already
        # delivered with the same normalized recipient and delivered_on keeps
        # its record untouched; one whose stored delivery disagrees -- or is
        # not an object, misses fields or carries invalid text/date -- rejects
        # the whole request before anything is signed. Lines, amounts,
        # shipment, stock, reservations, carts and existing returns are never
        # touched, returns never block signing, and no stock events are added.
        carrier = text(carrier, "carrier")
        tracking_no = text(tracking_no, "tracking_no")
        recipient = text(recipient, "recipient")
        delivered_on = calendar_date(delivered_on, "delivered_on")
        data = self._read()
        matched = []
        for order in data.get("orders", {}).values():
            if order.get("status") not in ("shipped", "delivered"):
                continue
            shipment = order.get("shipment")
            # A missing or non-object shipment, or one whose required fields
            # are missing, non-string or blank, simply cannot match.
            if not isinstance(shipment, dict):
                continue
            current_carrier = shipment.get("carrier")
            current_tracking = shipment.get("tracking_no")
            if not isinstance(current_carrier, str) or not isinstance(current_tracking, str):
                continue
            if current_carrier.strip() != carrier or current_tracking.strip() != tracking_no:
                continue
            matched.append(order)
        if not matched:
            raise ValueError("no orders match this shipment")
        matched.sort(key=lambda order: order["order_id"])
        # Check every already-delivered match against the request before
        # mutating anything, so a conflicting or malformed stored delivery
        # leaves the file, sequences and every other order untouched.
        pending = []
        for order in matched:
            if order["status"] != "delivered":
                pending.append(order)
                continue
            delivery = order.get("delivery")
            if not isinstance(delivery, dict):
                raise ValueError("stored delivery must be an object with recipient and delivered_on")
            if (text(delivery.get("recipient"), "recipient") != recipient
                    or calendar_date(delivery.get("delivered_on"), "delivered_on") != delivered_on):
                raise ValueError("stored delivery does not match the request: " + order["order_id"])
        if pending:
            # Mixed state: only the not-yet-delivered orders are signed; when
            # every match was already signed with the same info the result is
            # still returned but no file is written and no event is appended.
            for order in pending:
                order["status"] = "delivered"
                order["delivery"] = {"recipient": recipient, "delivered_on": delivered_on}
                self._record_event(data, order["order_id"], "confirm-delivery", order, False)
            self._write(data)
        return {"carrier": carrier, "tracking_no": tracking_no, "orders": matched}

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

    def reservation_audit(self, sku):
        # Read-only per-product reconciliation: the stock view's reserved total
        # versus the reservations actually held by the placed orders whose
        # current lines contain the sku. It never writes, never fabricates
        # reservations from deal content or history, and never corrects a
        # mismatch -- a legacy gap is reported as the actual difference.
        sku = text(sku, "sku")
        data = self._read()
        if sku not in data.get("products", {}):
            raise ValueError("unknown product: " + sku)
        # Reuse the stock query itself so the embedded view is always identical
        # to a standalone stock call, including the unmanaged null semantics.
        stock = self.stock(sku)
        managed = sku in data.get("inventory", {})
        rows = []
        allocated = 0
        for order in data.get("orders", {}).values():
            if order["status"] != "placed":
                continue
            quantity = 0
            for line in order["lines"]:
                if line["sku"] == sku:
                    quantity += line["quantity"]
            if not quantity:
                continue
            order_id = order["order_id"]
            if managed:
                reserved = data.get("reservations", {}).get(order_id, {}).get(sku, 0)
            else:
                # Unmanaged products (including legacy data without inventory
                # records) hold no reservations, regardless of stray records.
                reserved = 0
            rows.append({
                "order_id": order_id,
                "quantity": quantity,
                "reserved": reserved,
                "unreserved": max(0, quantity - reserved),
            })
            allocated += reserved
        rows.sort(key=lambda row: row["order_id"])
        # Keep the sign: positive means stock holds reservations not detailed
        # by the displayed orders, negative means the details exceed the books.
        return {
            "stock": stock,
            "allocated": allocated,
            "difference": stock["reserved"] - allocated,
            "orders": rows,
        }

    def list_orders(self):
        return sorted(self._read().get("orders", {}).values(), key=lambda x: x["order_id"])

    def quote_return(self, order_id, lines):
        # Read-only return preview: amounts come from the order's stored deal
        # prices, never the catalog, and paused sales, missing stock, unmanaged
        # or catalog-less products never block original-order items. It never
        # writes, never creates the data directory, never occupies return
        # allowance, and a later record_return still validates against the
        # data current at that time.
        order_id = text(order_id, "order_id")
        requested, _ = self._merged_return_lines(lines, "lines")
        data = self._read()
        order = data.get("orders", {}).get(order_id)
        if order is None:
            raise ValueError("unknown order: " + order_id)
        if order["status"] not in ("shipped", "delivered"):
            raise ValueError("only a shipped or delivered order can quote a return")
        ordered = {}
        deal_prices = {}
        for line in order["lines"]:
            sku = line["sku"]
            ordered[sku] = ordered.get(sku, 0) + line["quantity"]
            deal_prices.setdefault(sku, []).append(line.get("unit_price_cents"))
        # Active registrations occupy allowance, received ones included;
        # cancelled records free it and amended records count at their latest
        # quantities. Legacy data without any returns reads as nothing
        # returned and is never backfilled.
        returned = {}
        for record in data.get("returns", {}).get(order_id, []):
            for line in record["lines"]:
                returned[line["sku"]] = returned.get(line["sku"], 0) + line["quantity"]
        result_lines = []
        can_record = True
        for sku in sorted(requested):
            if sku not in ordered:
                raise ValueError("sku not in original order: " + sku)
            prices = deal_prices[sku]
            unit_price = prices[0]
            if type(unit_price) is not int or unit_price < 0:
                raise ValueError("stored unit price is missing or invalid: " + sku)
            if any(price != unit_price for price in prices):
                raise ValueError("inconsistent unit prices in order: " + sku)
            quantity = requested[sku]
            remaining = ordered[sku] - returned.get(sku, 0)
            if quantity > remaining:
                # Over-quantity is reported, not truncated: the full requested
                # amount is still priced.
                can_record = False
            result_lines.append({
                "sku": sku,
                "quantity": quantity,
                "unit_price_cents": unit_price,
                "subtotal_cents": quantity * unit_price,
                "remaining": remaining,
            })
        return {
            "order_id": order_id,
            "lines": result_lines,
            "total_cents": sum(line["subtotal_cents"] for line in result_lines),
            "can_record": can_record,
        }

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

    @staticmethod
    def _merged_return_lines(value, label):
        # Normalizes a return line list: a nonempty list of objects each with a
        # trimmed nonempty sku and a positive integer quantity (booleans
        # rejected); extra fields are ignored and duplicate skus merge. Returns
        # (merged, lines) where merged maps sku -> quantity and lines is the
        # sorted sku/quantity list used for comparison, storage and results.
        if not isinstance(value, list) or not value:
            raise ValueError(label + " must be a nonempty list")
        merged = {}
        for line in value:
            if not isinstance(line, dict):
                raise ValueError("each line must be an object with sku and quantity")
            sku = text(line.get("sku"), "sku")
            quantity = positive(line.get("quantity"), "quantity")
            merged[sku] = merged.get(sku, 0) + quantity
        return merged, [{"sku": sku, "quantity": merged[sku]} for sku in sorted(merged)]

    def amend_return(self, return_id, expected_lines, lines):
        # Whole-registration correction of a pending return: the registration
        # keeps its return_id and order, but its lines are replaced wholesale
        # once the expected original content matches the current record. The
        # new list may grow, shrink, drop or add skus as long as every sku
        # belongs to the original order; paused sales, unmanaged stock and a
        # missing catalog entry never block original-order products. The
        # merged ordered quantity is the cap: the new quantity plus every
        # other active registration of the same order (received ones included)
        # must not exceed it, while this registration's old content and
        # cancelled registrations occupy no allowance.
        return_id = text(return_id, "return_id")
        expected, _ = self._merged_return_lines(expected_lines, "expected_lines")
        requested, after = self._merged_return_lines(lines, "lines")
        data = self._read()
        located = self._find_return(data, return_id)
        if located is None:
            raise ValueError("unknown return: " + return_id)
        order_id, record, cancelled = located
        if cancelled:
            raise ValueError("return already cancelled: " + return_id)
        order = data.get("orders", {}).get(order_id)
        if order is None or order["status"] not in ("shipped", "delivered"):
            raise ValueError("only a shipped order can amend a return: " + order_id)
        if return_id in data.get("return_receipts", {}):
            raise ValueError("return already received: " + return_id)
        current = {}
        for line in record["lines"]:
            current[line["sku"]] = current.get(line["sku"], 0) + line["quantity"]
        # The original content is always checked first, even when the target
        # equals the current content.
        if expected != current:
            raise ValueError("expected lines do not match current lines")
        ordered = {}
        for line in order["lines"]:
            ordered[line["sku"]] = ordered.get(line["sku"], 0) + line["quantity"]
        others = {}
        for other in data.get("returns", {}).get(order_id, []):
            if other is record:
                continue
            for line in other["lines"]:
                others[line["sku"]] = others.get(line["sku"], 0) + line["quantity"]
        for sku, quantity in requested.items():
            if sku not in ordered:
                raise ValueError("sku not in original order: " + sku)
            if others.get(sku, 0) + quantity > ordered[sku]:
                raise ValueError("returned quantity exceeds ordered quantity: " + sku)
        before = [{"sku": sku, "quantity": current[sku]} for sku in sorted(current)]
        result = {"order_id": order_id, "return_id": return_id, "before": before, "after": after}
        if after == before:
            # Original content matched but the target changes nothing: still
            # return the result, but no file is written and no event is
            # appended.
            return result
        record["lines"] = after
        self._record_event(data, order_id, "amend-return", result, False)
        self._write(data)
        return result

    def receive_return(self, return_id):
        return_id = text(return_id, "return_id")
        data = self._read()
        result = self._receive_return(data, return_id)
        self._write(data)
        return result

    def receive_return_batch(self, return_ids):
        # All-or-nothing batch receive: every id is normalized and every
        # registration is received inside `data` before the single write, so a
        # rejected batch leaves the file, stock, receipts, history and
        # sequences untouched and never creates the data directory.
        if not isinstance(return_ids, list) or not return_ids:
            raise ValueError("return_ids must be a nonempty list")
        selected = []
        seen = set()
        for return_id in return_ids:
            return_id = text(return_id, "return_id")
            if return_id in seen:
                raise ValueError("duplicate return_id: " + return_id)
            seen.add(return_id)
            selected.append(return_id)
        data = self._read()
        results = []
        # Apply in input order so shared-product stock snapshots chain; a
        # failure anywhere (including the last entry) discards every in-memory
        # change because the file is only written once the whole batch has
        # succeeded.
        for return_id in selected:
            results.append(self._receive_return(data, return_id))
        self._write(data)
        return sorted(results, key=lambda receipt: receipt["return_id"])

    def _receive_return(self, data, return_id):
        # Shared mutation for receive_return and receive_return_batch: the
        # caller has normalized the id and writes `data` once everything has
        # succeeded.
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
        return result

    def get_return_receipt(self, return_id):
        return_id = text(return_id, "return_id")
        receipt = self._read().get("return_receipts", {}).get(return_id)
        if receipt is None:
            raise ValueError("return has not been received: " + return_id)
        return copy.deepcopy(receipt)

    def cancel_received_return(self, return_id):
        # Whole-reversal of an already received return: every product's current
        # on_hand is reduced by the quantity the original receipt added, so
        # restocks, shipments and stock counts after the receive are preserved.
        # Available drops by the same amount; reserved and every order's
        # reservation records are never touched. The registration becomes
        # cancelled (freeing returnable quantity) but its id stays occupied
        # for the whole root, the original receipt stays queryable, and order
        # status, deal lines, amounts, shipment and delivery are preserved.
        # Paused sales never block the reversal; no refund happens.
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
            raise ValueError("only a shipped order can cancel a received return: " + order_id)
        receipt = data.get("return_receipts", {}).get(return_id)
        if receipt is None:
            # Legacy registrations without a receipt read as not received.
            raise ValueError("return has not been received: " + return_id)
        products = data.get("products", {})
        inventory = data.get("inventory", {})
        quantities = {}
        for line in receipt["lines"]:
            quantities[line["sku"]] = quantities.get(line["sku"], 0) + line["quantity"]
        # Validate every product and every resulting on_hand against the
        # current stock before touching any inventory entry, so a rejected
        # reversal leaves stock, returns, receipts, history and sequences
        # untouched and never creates the data directory.
        result_lines = []
        for sku in sorted(quantities):
            if sku not in products:
                raise ValueError("unknown product: " + sku)
            entry = inventory.get(sku)
            if entry is None:
                raise ValueError("product is not managed: " + sku)
            quantity = quantities[sku]
            on_hand = entry["on_hand"] - quantity
            if on_hand < 0:
                raise ValueError("reversed on_hand cannot be negative: " + sku)
            reserved = entry["reserved"]
            if on_hand < reserved:
                raise ValueError("reversed on_hand cannot be below reserved quantity: " + sku)
            result_lines.append({
                "sku": sku,
                "quantity": quantity,
                "before": self._stock_view(sku, entry),
                "after": {"sku": sku, "on_hand": on_hand, "reserved": reserved, "available": on_hand - reserved},
            })
        for line in result_lines:
            inventory[line["sku"]]["on_hand"] = line["after"]["on_hand"]
            self._record_stock_event(
                data, line["sku"], "cancel-received-return", return_id, line["before"], line["after"]
            )
        result = {"order_id": order_id, "return_id": return_id, "lines": result_lines}
        records = data["returns"][order_id]
        records.remove(record)
        if not records:
            del data["returns"][order_id]
        data.setdefault("cancelled_returns", {}).setdefault(order_id, []).append(copy.deepcopy(record))
        self._record_event(data, order_id, "cancel-received-return", result, False)
        self._write(data)
        return result

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

    def order_progress(self, order_id):
        # Read-only single-order fulfillment overview: the full order and
        # history results (identical to get/history) plus per-sku fulfillment
        # quantities. It never writes, never fabricates reservations, returns or
        # receipts from history, and missing legacy collections read as empty.
        order_id = text(order_id, "order_id")
        data = self._read()
        order = data.get("orders", {}).get(order_id)
        if order is None:
            raise ValueError("unknown order: " + order_id)
        status = order["status"]
        shipped = status in ("shipped", "delivered")
        ordered = {}
        for line in order["lines"]:
            ordered[line["sku"]] = ordered.get(line["sku"], 0) + line["quantity"]
        inventory = data.get("inventory", {})
        own = data.get("reservations", {}).get(order_id, {})
        # Active registrations only: cancelled records live in a separate
        # bucket and never count. An active record counts as received when it
        # holds a receipt, otherwise pending; an amended registration's current
        # lines already are its latest list.
        pending = {}
        received = {}
        if shipped:
            receipts = data.get("return_receipts", {})
            for record in data.get("returns", {}).get(order_id, ()):
                bucket = received if record["return_id"] in receipts else pending
                for line in record["lines"]:
                    bucket[line["sku"]] = bucket.get(line["sku"], 0) + line["quantity"]
        lines = []
        for sku in sorted(ordered):
            quantity = ordered[sku]
            # Managed-ness follows the inventory records: stray reservations
            # for an unmanaged sku read as zero. Only placed orders still hold
            # reservations; other statuses read both reservation fields zero.
            if status == "placed" and sku in inventory:
                reserved = own.get(sku, 0)
                needed = max(0, quantity - reserved)
            else:
                reserved = 0
                needed = 0
            shipped_qty = quantity if shipped else 0
            pending_qty = pending.get(sku, 0)
            received_qty = received.get(sku, 0)
            remaining = quantity - pending_qty - received_qty if shipped else 0
            lines.append({
                "sku": sku,
                "ordered": quantity,
                "reserved": reserved,
                "needed": needed,
                "shipped": shipped_qty,
                "pending": pending_qty,
                "received": received_qty,
                "remaining": remaining,
                "net": shipped_qty - received_qty,
            })
        return {"order": self.get(order_id), "history": self.history(order_id), "lines": lines}

    def shipment_orders(self, carrier, tracking_no):
        # Read-only lookup of every order currently shipped under a carrier +
        # tracking number combination: only the order's current shipment object
        # is matched, never the ship/correct-shipment snapshots in history, and
        # only shipped or delivered orders qualify. Each match embeds the full
        # order-progress result; the merged lines sum that view's fulfillment
        # quantities. It never writes, never fabricates shipment info for legacy
        # orders, and missing orders/history/returns/receipts collections read
        # as empty.
        carrier = text(carrier, "carrier")
        tracking_no = text(tracking_no, "tracking_no")
        data = self._read()
        orders = data.get("orders", {})
        matched = []
        for order in orders.values():
            if order.get("status") not in ("shipped", "delivered"):
                continue
            shipment = order.get("shipment")
            # A missing or non-object shipment, or one whose required fields are
            # missing, non-string or blank, simply cannot match: skip it rather
            # than raising or fabricating info.
            if not isinstance(shipment, dict):
                continue
            current_carrier = shipment.get("carrier")
            current_tracking = shipment.get("tracking_no")
            if not isinstance(current_carrier, str) or not isinstance(current_tracking, str):
                continue
            current_carrier = current_carrier.strip()
            current_tracking = current_tracking.strip()
            if not current_carrier or not current_tracking:
                continue
            if current_carrier != carrier or current_tracking != tracking_no:
                continue
            matched.append(order)
        matched.sort(key=lambda order: order["order_id"])
        progress = [self.order_progress(order["order_id"]) for order in matched]
        totals = {}
        for result in progress:
            for line in result["lines"]:
                row = totals.setdefault(line["sku"], {"shipped": 0, "pending": 0,
                                                      "received": 0, "remaining": 0, "net": 0})
                row["shipped"] += line["shipped"]
                row["pending"] += line["pending"]
                row["received"] += line["received"]
                row["remaining"] += line["remaining"]
                row["net"] += line["net"]
        lines = [{"sku": sku, **totals[sku]} for sku in sorted(totals)]
        return {"carrier": carrier, "tracking_no": tracking_no,
                "orders": progress, "lines": lines}

    def fulfillment_summary(self):
        # Read-only root-wide fulfillment money summary: only orders whose
        # current status is shipped or delivered count; carts and every other
        # order never do. Shipped quantities equal ordered quantities (never
        # stock deductions); active returns count at their latest lines, split
        # into received (with a receipt) and pending (without), cancelled
        # registrations excluded. Amounts use each order's stored deal price
        # per line, never the current catalog price. Any included order whose
        # line misses a deal price, carries a negative/non-integer/boolean
        # price or inconsistent prices for one sku rejects the whole query. It
        # never writes, never creates the directory and never consumes a
        # sequence; missing legacy collections read as empty.
        data = self._read()
        orders = data.get("orders", {})
        receipts = data.get("return_receipts", {})
        selected = [order for order in orders.values()
                    if order.get("status") in ("shipped", "delivered")]
        totals = {}
        order_count = 0
        for order in selected:
            order_id = order["order_id"]
            order_count += 1
            ordered = {}
            deal_prices = {}
            for line in order["lines"]:
                sku = line["sku"]
                ordered[sku] = ordered.get(sku, 0) + line["quantity"]
                deal_prices.setdefault(sku, []).append(line.get("unit_price_cents"))
            # Validate every deal price of every included order before any
            # money is computed, so one bad legacy line rejects the whole
            # summary instead of surfacing partial totals.
            prices = {}
            for sku, sku_prices in deal_prices.items():
                unit_price = sku_prices[0]
                if type(unit_price) is not int or unit_price < 0:
                    raise ValueError("stored unit price is missing or invalid: " + sku)
                if any(price != unit_price for price in sku_prices):
                    raise ValueError("inconsistent unit prices in order: " + sku)
                prices[sku] = unit_price
            pending = {}
            received = {}
            for record in data.get("returns", {}).get(order_id, ()):
                bucket = received if record["return_id"] in receipts else pending
                for line in record["lines"]:
                    bucket[line["sku"]] = bucket.get(line["sku"], 0) + line["quantity"]
            for sku, quantity in ordered.items():
                pending_qty = pending.get(sku, 0)
                received_qty = received.get(sku, 0)
                net_qty = quantity - received_qty
                unit_price = prices[sku]
                row = totals.setdefault(sku, {
                    "shipped": 0, "pending": 0, "received": 0, "net": 0,
                    "shipped_cents": 0, "pending_cents": 0,
                    "received_cents": 0, "net_cents": 0,
                })
                row["shipped"] += quantity
                row["pending"] += pending_qty
                row["received"] += received_qty
                row["net"] += net_qty
                # Each order's quantities are priced at that order's stored
                # deal price, so the same sku may add different cents across
                # orders; the cross-order sum stays an integer.
                row["shipped_cents"] += quantity * unit_price
                row["pending_cents"] += pending_qty * unit_price
                row["received_cents"] += received_qty * unit_price
                row["net_cents"] += net_qty * unit_price
        lines = []
        summary_totals = {
            "shipped": 0, "pending": 0, "received": 0, "net": 0,
            "shipped_cents": 0, "pending_cents": 0,
            "received_cents": 0, "net_cents": 0,
        }
        for sku in sorted(totals):
            row = totals[sku]
            lines.append({"sku": sku, **row})
            for field in summary_totals:
                summary_totals[field] += row[field]
        return {"order_count": order_count, "lines": lines, "totals": summary_totals}

    def replenishment_report(self):
        # Read-only root-wide replenishment list: per managed sku it merges the
        # reservation demand placed orders still lack with the pending returns
        # that could be received back into stock. Carts, non-placed orders and
        # unmanaged products never contribute demand; received or cancelled
        # returns never contribute projected restock. It never writes, never
        # creates the directory, never consumes a sequence, and never performs
        # a restock, reservation or receive -- the two shortfalls are advisory.
        data = self._read()
        orders = data.get("orders", {})
        products = data.get("products", {})
        inventory = data.get("inventory", {})
        all_reservations = data.get("reservations", {})
        # Duplicate skus in an order merge first; actual reservations come from
        # the current attribution records (missing records read zero). Only
        # managed skus and orders still short of reservation are kept.
        demand = {}  # sku -> reservation-audit-style order rows
        for order_id in sorted(orders):
            order = orders[order_id]
            if order["status"] != "placed":
                continue
            merged = {}
            for line in order["lines"]:
                merged[line["sku"]] = merged.get(line["sku"], 0) + line["quantity"]
            own = all_reservations.get(order_id, {})
            for sku, quantity in merged.items():
                if sku not in inventory:
                    continue
                reserved = own.get(sku, 0)
                unreserved = max(0, quantity - reserved)
                if unreserved <= 0:
                    continue
                demand.setdefault(sku, []).append({
                    "order_id": order_id,
                    "quantity": quantity,
                    "reserved": reserved,
                    "unreserved": unreserved,
                })
        # A listed product is managed by definition, so a legacy inventory
        # record without a catalog entry rejects the whole query.
        for sku in sorted(demand):
            if sku not in products:
                raise ValueError("unknown product: " + sku)
        included = set(demand)
        # Pending returns reuse the return-worklist pending entry shape. A
        # registration is attached to every included sku its latest lines
        # touch; blocked registrations stay visible but contribute no pending
        # quantity for any sku (the whole registration is excluded).
        entries_by_sku = {sku: [] for sku in included}
        pending_qty = {sku: 0 for sku in included}
        receipts = data.get("return_receipts", {})
        for records in data.get("returns", {}).values():
            for record in records:
                if record["return_id"] in receipts:
                    continue
                quantities = {}
                for line in record["lines"]:
                    quantities[line["sku"]] = quantities.get(line["sku"], 0) + line["quantity"]
                touched = sorted(sku for sku in quantities if sku in included)
                if not touched:
                    continue
                order = orders.get(record["order_id"])
                if order is None or order["status"] not in ("shipped", "delivered"):
                    if order is None:
                        raise ValueError("unknown order: " + record["order_id"])
                    raise ValueError(
                        "only a shipped or delivered order can back a return: " + record["order_id"]
                    )
                blockers = []
                for sku in sorted(quantities):
                    if sku not in products:
                        blockers.append({"sku": sku, "reason": "unknown-product"})
                    elif sku not in inventory:
                        blockers.append({"sku": sku, "reason": "unmanaged"})
                entry = {
                    "order_id": record["order_id"],
                    "return_id": record["return_id"],
                    "stage": "pending",
                    "lines": [{"sku": sku, "quantity": quantities[sku]} for sku in sorted(quantities)],
                    "can_receive": not blockers,
                    "blockers": blockers,
                }
                for sku in touched:
                    entries_by_sku[sku].append(entry)
                    if entry["can_receive"]:
                        pending_qty[sku] += quantities[sku]
        report = []
        for sku in sorted(included):
            rows = sorted(demand[sku], key=lambda row: row["order_id"])
            needed = sum(row["unreserved"] for row in rows)
            returns = sorted(entries_by_sku[sku], key=lambda entry: entry["return_id"])
            # Reuse the stock query itself so the embedded view is always
            # identical to a standalone stock call.
            stock = self.stock(sku)
            available = stock["available"]
            pending = pending_qty[sku]
            report.append({
                "sku": sku,
                "stock": stock,
                "needed": needed,
                "pending": pending,
                "shortfall": max(0, needed - available),
                "projected_shortfall": max(0, needed - available - pending),
                "orders": rows,
                "returns": returns,
            })
        return report

    def order_worklist(self, stage="open"):
        # Read-only cross-order fulfillment worklist: every order's open tasks
        # (reserve top-up, ship, deliver, receive-return) derived from its
        # current state, with the full order-progress result embedded. It never
        # writes, never creates the data directory, never consumes a sequence,
        # and never fabricates reservations, returns or receipts from history;
        # missing legacy collections read as empty.
        if not isinstance(stage, str):
            raise ValueError("stage must be one of: open, all, reserve, ship, deliver, receive-return")
        stage = stage.strip()
        if stage not in ("open", "all", "reserve", "ship", "deliver", "receive-return"):
            raise ValueError("stage must be one of: open, all, reserve, ship, deliver, receive-return")
        data = self._read()
        orders = data.get("orders", {})
        entries = []
        for order_id in sorted(orders):
            progress = self.order_progress(order_id)
            status = progress["order"]["status"]
            lines = progress["lines"]
            tasks = []
            if status == "placed":
                # A placed order still needs a reservation top-up when any
                # managed product's needed (order-progress semantics: ordered
                # minus actually reserved, never netted against availability)
                # is positive; unmanaged products never create one. Otherwise
                # it is ready to ship.
                if any(line["needed"] > 0 for line in lines):
                    tasks.append("reserve")
                else:
                    tasks.append("ship")
            elif status == "shipped":
                tasks.append("deliver")
            # An active registration without a receipt keeps the receive-return
            # task for shipped and delivered orders alike, even when its
            # products are paused, unmanaged or gone from the catalog; pending
            # quantities in the progress lines already exclude cancelled
            # records and count amended ones at their latest content.
            if status in ("shipped", "delivered") and any(line["pending"] > 0 for line in lines):
                tasks.append("receive-return")
            if stage == "all" or (stage == "open" and tasks) or stage in tasks:
                entries.append({"order_id": order_id, "tasks": tasks, "progress": progress})
        return entries

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
