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
- `amend` → `OrderDesk.amend(order_id, lines)`。替换 `placed` 订单的全部商品行，不另建订单，编号与状态不变。`lines` 结构同 `place`，行内额外字段忽略；商品行保留输入顺序与重复 SKU，单价取提交时商品目录，小计与总金额仍为整数分，`get` 与 `list` 返回修改后的内容。同一 SKU 合并数量检查：新需求不得超过当前可用量加该订单实际预留量；成功后各商品预留量等于其他订单预留加本次需求，在库量不变，移除的商品释放其原预留，此后取消或发货只处理修改后的实际预留。未纳管商品仍不限制数量、不生成预留；下单后才纳管的商品按当前库存检查并预留；无预留记录的旧订单不获得额外额度。订单不存在或非 `placed`、编号或 SKU 非法、清单非列表或为空、行非对象或缺少 `sku`/`quantity`、数量非正整数或为布尔值、商品未知、库存不足均抛出 `ValueError`，拒绝不改写数据、不消耗历史序号。每次成功提交（含相同清单重复提交）追加 `action` 为 `amend`、`result` 为修改后订单快照的历史事件，旧事件与 `complete` 不变，无历史的旧订单从 1 开始且 `complete` 为 `false`；修改与历史同次写入 `root/data.json`。
- `quote` → `OrderDesk.quote(lines)`。下单前预览，不创建订单、不预留库存、不写任何文件。`lines` 为非空列表，每行含 `sku`（去除首尾空白后非空字符串，区分大小写）和正整数 `quantity`（不接受布尔值），行内额外字段忽略；清单非列表或为空、行不是对象、缺少必要字段、sku 非法、数量为零/负数/非整数、商品不存在均抛出 `ValueError`，整次预览不返回部分结果。相同 sku 合并数量，商品行按 sku 升序；返回仅含 `lines`、`total_cents`、`can_place`，每行仅含 `sku`、`quantity`、`unit_price_cents`、`subtotal_cents`、`available`、`shortfall`。单价只取当前目录，小计为合并数量乘单价，缺货商品和零价商品都保留。纳管商品 `available` 为在库量减预留量，`shortfall` 为需求超过可用量的部分（未超过为零）；未纳管商品（含无库存字段的旧数据）`available` 为 `null`、`shortfall` 为零。所有行缺口为零时 `can_place` 为 `true`。预览不锁定价格或库存，随后交给 `place` 时仍按当时数据校验和预留。
- `get` → `OrderDesk.get(...)`。参数名见 `core.py` 的公开方法签名。
- `cancel` → `OrderDesk.cancel(...)`。取消只释放该订单实际预留的数量，在库量不变。
- `ship` → `OrderDesk.ship(order_id, carrier, tracking_no)`。一次性整单发货：状态改为 `shipped`，订单新增仅含 `carrier`、`tracking_no`（均去除首尾空白）的 `shipment`。按该订单实际预留的数量同时扣减在库量与预留量，可用量不变；未纳管商品、下单后才纳管的商品及无预留记录的旧订单不扣库存。订单不存在、已取消或已发货均报错，重复发货不覆盖记录或再次扣减。
- `record-return` → `OrderDesk.record_return(order_id, return_id, lines)`。分次退货登记，仅接受 `shipped` 订单；只登记退货，不退款、不回补库存，订单状态、金额、商品行、shipment 与其他订单预留量均不变。退货编号在整个 root 内唯一，重复提交（即使内容相同）拒绝且不覆盖。订单编号、退货编号与 SKU 均须为非空字符串（去除首尾空白，区分大小写）；`lines` 为非空列表，每行含 `sku` 和正整数 `quantity`（布尔值不接受）。本次输入及原订单中的重复 SKU 分别合并数量，累计成功退货量加本次数量不得超过原订量，任一 SKU 超量或不属于原订单则整次登记拒绝。成功返回仅含 `order_id`、`return_id`、`lines` 的记录，商品行按 sku 升序，每项仅含 `sku`、`quantity`。
- `returns` → `OrderDesk.get_returns(order_id)`。返回 `{order_id, records, remaining}`：`records` 按 return_id 升序，`remaining` 按 sku 升序覆盖原订单全部商品，数量为原订量减累计退货量，零也保留。已有订单均可查询，无退货记录时 `records` 为空；查询不写文件。
- `history` → `OrderDesk.history(order_id)`。按实际发生顺序返回订单过程，仅含 `order_id`、`status`、`complete`、`events`：`status` 为订单当前状态；`complete` 表示历史是否包含真实创建事件。`events` 按成功操作先后排列，每项仅含 `sequence`、`action`、`result`；`sequence` 在每个订单内从 1 连续递增，`action` 为 `place`、`amend`、`cancel`、`ship`、`record-return`，`result` 保存对应公开方法成功返回内容的快照（创建保留成交商品行与金额，发货保留 shipment，退货保留归并后的记录），后续操作不改写旧快照。操作失败不新增事件、不消耗序号；退货编号即使逆序提交，历史仍按登记顺序显示。历史随业务数据保存在 `root/data.json`，与业务变更同次写入，重新打开同一 root 结果一致。无历史数据的旧订单查询时 `events` 为空、`complete` 为 `false`，不依据状态或已有退货记录补造；这类订单随后成功取消、发货或登记退货时从 1 开始记录新事件，`complete` 仍为 `false`，新创建订单为 `true`。`order_id` 须为去除首尾空白后的非空字符串（区分大小写），非字符串、空白编号及未知订单均抛出 `ValueError`；查询不创建目录或写文件。
- `count-stock` → `OrderDesk.count_stock(count_id, lines)`。库存盘点登记，用实际清点数量校准已纳管商品的在库量。`count_id` 为去除首尾空白后的非空字符串（区分大小写），在 root 内唯一，重复提交（即使内容相同）拒绝且不覆盖。`lines` 为非空列表，每行含 `sku`（去除首尾空白后非空字符串，区分大小写）和非负整数 `on_hand`（含零，不接受布尔值），行内额外字段忽略；同一盘点中重复 sku 拒绝，不合并数量。每个商品必须存在且已纳管（至少成功补货一次），新在库量不得低于当前预留量；任一商品未知、未纳管或低于预留量则整次盘点拒绝。成功后一次性替换本次全部商品的在库量，预留量及各订单实际预留记录保留，未列出商品的在库量与预留量均不变。返回仅含 `count_id` 和 `lines`，商品行按 sku 升序，每行仅含 `sku`、`before`、`after`、`delta`；`before` 与 `after` 沿用 stock 的完整库存结构（`{sku, on_hand, reserved, available}`），`delta` 为盘点后在库量减盘点前在库量。盘点记录与库存变更同次写入 `root/data.json`。
- `stock-count` → `OrderDesk.get_stock_count(count_id)`。返回登记时的盘点快照，仅含 `count_id` 和 `lines`；后续补货、下单、取消、发货或再次盘点均不改写该快照，重新打开同一 root 结果一致。`count_id` 须为去除首尾空白后的非空字符串（区分大小写），非字符串、空白编号及不存在的编号均抛出 `ValueError`；查询不创建目录或写文件。无盘点记录的旧数据可正常使用，不补造记录。
- `list` → `OrderDesk.list_orders(...)`。参数名见 `core.py` 的公开方法签名。

命令成功向标准输出打印 JSON 并返回 0；输入或本地文件错误向标准错误输出说明并返回 2。无参数的方法可省略输入文件。数据保存在 `root/data.json`，每次成功修改后保存；适用于单进程本地使用。

## 样例

`examples/` 提供 3 份虚构业务样例。`tests/` 覆盖业务路径、拒绝非法操作后的状态和命令入口。

## 当前边界

当前仅支持一币种、一个商品目录、一次性取消和发货、分次退货登记（不退款、不回补库存）、按商品的库存预留及库存盘点登记。没有支付、拆单或物流联网功能。 不承诺并发写入或断电恢复。
