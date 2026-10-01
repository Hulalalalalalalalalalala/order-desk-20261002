from .storage import JsonStore, text, positive

class OrderDesk(JsonStore):
    def add_product(self, sku, name, price_cents):
        sku, name = text(sku, "sku"), text(name, "name")
        if type(price_cents) is not int or price_cents < 0:
            raise ValueError("price_cents must be a nonnegative integer")
        data = self._read()
        products = data.setdefault("products", {})
        if sku in products:
            raise ValueError("product already exists")
        product = {"sku": sku, "name": name, "price_cents": price_cents}
        products[sku] = product
        self._write(data)
        return product

    def restock(self, sku, quantity):
        sku = text(sku, "sku")
        quantity = positive(quantity, "quantity")
        data = self._read()
        if sku not in data.get("products", {}):
            raise ValueError("unknown product: " + sku)
        record = data.setdefault("inventory", {}).setdefault(sku, {"on_hand": 0, "reserved": 0})
        record["on_hand"] += quantity
        self._write(data)
        return self._stock_view(sku, record)

    def stock(self, sku):
        sku = text(sku, "sku")
        data = self._read()
        if sku not in data.get("products", {}):
            raise ValueError("unknown product: " + sku)
        return self._stock_view(sku, data.get("inventory", {}).get(sku))

    @staticmethod
    def _stock_view(sku, record):
        if record is None:
            return {"sku": sku, "on_hand": None, "reserved": 0, "available": None}
        return {"sku": sku, "on_hand": record["on_hand"], "reserved": record["reserved"], "available": record["on_hand"] - record["reserved"]}

    def place(self, order_id, lines):
        order_id = text(order_id, "order_id")
        if not isinstance(lines, list) or not lines:
            raise ValueError("lines must be a nonempty list")
        data = self._read()
        if order_id in data.get("orders", {}):
            raise ValueError("order already exists")
        items = []
        totals = {}
        for line in lines:
            sku, quantity = text(line["sku"], "sku"), positive(line["quantity"], "quantity")
            product = data.get("products", {}).get(sku)
            if product is None:
                raise ValueError("unknown product: " + sku)
            items.append({"sku": sku, "quantity": quantity, "unit_price_cents": product["price_cents"], "subtotal_cents": quantity * product["price_cents"]})
            totals[sku] = totals.get(sku, 0) + quantity
        inventory = data.get("inventory", {})
        for sku, quantity in totals.items():
            record = inventory.get(sku)
            if record is not None and record["on_hand"] - record["reserved"] < quantity:
                raise ValueError("insufficient stock: " + sku)
        reservation = {}
        for sku, quantity in totals.items():
            if sku in inventory:
                inventory[sku]["reserved"] += quantity
                reservation[sku] = quantity
        order = {"order_id": order_id, "status": "placed", "lines": items, "total_cents": sum(x["subtotal_cents"] for x in items)}
        data.setdefault("orders", {})[order_id] = order
        if reservation:
            data.setdefault("reservations", {})[order_id] = reservation
        self._write(data)
        return order

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
        reservation = data.get("reservations", {}).pop(order_id, None)
        if reservation:
            inventory = data.get("inventory", {})
            for sku, quantity in reservation.items():
                inventory[sku]["reserved"] -= quantity
        order["status"] = "cancelled"
        self._write(data)
        return order

    def list_orders(self):
        return sorted(self._read().get("orders", {}).values(), key=lambda x: x["order_id"])
