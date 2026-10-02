# 订单工作台

维护商品价格、计算订单金额，并保存订单与取消状态。金额使用整数分。

## 运行

需要 Python 3.10 或更新版本，仅使用标准库，无依赖安装步骤。请在本目录运行：

```sh
python3 -m order_desk --root ./state demo
python3 -m unittest discover -s tests -v
```

这是本地命令行程序，不监听网络端口，无账户或密码。`demo` 在指定 root 的临时子目录中读取 examples 样例并演示业务，结束后清理样例状态，不改变现有数据。

## 正常使用

公开 API：`from order_desk import OrderDesk`，然后 `OrderDesk(root)`。每个命令接收可选的 JSON 文件，其对象键与 API 方法参数一致。例如：

```sh
python3 -m order_desk --root ./state add-product examples/products.json
```

JSON 数组会按顺序执行多个独立操作；先前成功操作保留，后续失败不会回滚整批。重跑登记命令遇到已存在的标识会报错。

- `add-product` → `OrderDesk.add_product(...)`。参数名见 `core.py` 的公开方法签名。
- `restock` → `OrderDesk.restock(sku, quantity)`。补货只增加在库量，不改变预留量；商品首次成功补货后纳管库存。
- `stock` → `OrderDesk.stock(sku)`。返回 `{sku, on_hand, reserved, available}`；未纳管商品的 `on_hand`、`available` 为 `null`，`reserved` 为 `0`。
- `place` → `OrderDesk.place(...)`。对已纳管商品按合计数量检查并预留可用量（在库量减预留量），任一商品缺货则整笔订单不创建。
- `get` → `OrderDesk.get(...)`。参数名见 `core.py` 的公开方法签名。
- `cancel` → `OrderDesk.cancel(...)`。取消只释放该订单实际预留的数量，在库量不变。
- `ship` → `OrderDesk.ship(order_id, carrier, tracking_no)`。一次性整单发货：状态改为 `shipped`，订单新增仅含 `carrier`、`tracking_no`（均去除首尾空白）的 `shipment`。按该订单实际预留的数量同时扣减在库量与预留量，可用量不变；未纳管商品、下单后才纳管的商品及无预留记录的旧订单不扣库存。订单不存在、已取消或已发货均报错，重复发货不覆盖记录或再次扣减。
- `record-return` → `OrderDesk.record_return(order_id, return_id, lines)`。分次退货登记，仅接受 `shipped` 订单；只登记退货，不退款、不回补库存，订单状态、金额、商品行、shipment 与其他订单预留量均不变。退货编号在整个 root 内唯一，重复提交（即使内容相同）拒绝且不覆盖。订单编号、退货编号与 SKU 均须为非空字符串（去除首尾空白，区分大小写）；`lines` 为非空列表，每行含 `sku` 和正整数 `quantity`（布尔值不接受）。本次输入及原订单中的重复 SKU 分别合并数量，累计成功退货量加本次数量不得超过原订量，任一 SKU 超量或不属于原订单则整次登记拒绝。成功返回仅含 `order_id`、`return_id`、`lines` 的记录，商品行按 sku 升序，每项仅含 `sku`、`quantity`。
- `returns` → `OrderDesk.get_returns(order_id)`。返回 `{order_id, records, remaining}`：`records` 按 return_id 升序，`remaining` 按 sku 升序覆盖原订单全部商品，数量为原订量减累计退货量，零也保留。已有订单均可查询，无退货记录时 `records` 为空；查询不写文件。
- `history` → `OrderDesk.history(order_id)`。返回 `{order_id, status, complete, events}`：`status` 为订单当前状态；`complete` 表示历史中是否包含真实创建（place）事件；`events` 按成功操作先后排列，每项仅含 `sequence`、`action`、`result`，`sequence` 从 1 连续递增，`action` 为 `place`、`cancel`、`ship` 或 `record-return`，`result` 是对应公开方法成功返回内容的快照（创建保留成交商品行和金额，发货保留 shipment，退货保留归并后的退货记录），后续操作不改写旧快照。失败操作不新增事件、不消耗序号；退货编号逆序提交时历史仍按登记顺序显示，不同订单各自计数。历史与业务数据共同保存在 `root/data.json` 中。没有历史数据的旧订单查询时 `events` 为空、`complete` 为 `false`，不按状态或已有退货记录补造；这类订单之后成功取消、发货或登记退货时从 1 开始记录，`complete` 仍为 `false`。编号须为去除首尾空白的非空字符串，区分大小写；非字符串、空白编号或未知订单抛出 `ValueError`。查询不创建目录或写文件。
- `list` → `OrderDesk.list_orders(...)`。参数名见 `core.py` 的公开方法签名。

命令成功向标准输出打印 JSON 并返回 0；输入或本地文件错误向标准错误输出说明并返回 2。无参数的方法可省略输入文件。数据保存在 `root/data.json`，每次成功修改后保存；适用于单进程本地使用。

## 样例

`examples/` 提供 3 份虚构业务样例。`tests/` 覆盖业务路径、拒绝非法操作后的状态和命令入口。

## 当前边界

当前仅支持一币种、一个商品目录、一次性取消和发货、分次退货登记（不退款、不回补库存）及按商品的库存预留。没有支付、拆单或物流联网功能。 不承诺并发写入或断电恢复。
