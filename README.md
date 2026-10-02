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

- `add-product` → `OrderDesk.add_product(...)`。参数名见 `core.py` 的公开方法签名。新建商品默认启用销售（`enabled` 为 `true`），但新建时不在数据中写入 `enabled` 字段；仅在显式切换状态时才保存该字段。
- `set-product-enabled` → `OrderDesk.set_product_enabled(sku, enabled)`。暂停或恢复商品销售，仅返回 `{sku, name, price_cents, enabled}`。`sku` 去除首尾空白后匹配并区分大小写，非字符串、空白值或未知商品抛出 `ValueError`；`enabled` 必须是布尔值（`type` 为 `bool`），整数、字符串等不转换为布尔值，否则抛出 `ValueError`。结果与商品同次保存在 `root/data.json`，重新打开一致；重复设置同一状态仍成功；不改变名称、价格、库存、已有订单与历史，也不追加任何订单事件。旧商品缺少 `enabled` 字段时视为启用，设置时才写入该字段。
- `get-product` → `OrderDesk.get_product(sku)`。按 sku 查询商品，仅返回 `{sku, name, price_cents, enabled}`；缺少 `enabled` 字段的旧商品返回 `enabled` 为 `true`，但不补写字段。`sku` 规则同 `set-product-enabled`，非字符串、空白值或未知商品抛出 `ValueError`；查询不创建目录或文件。
- `restock` → `OrderDesk.restock(sku, quantity)`。补货只增加在库量，不改变预留量；商品首次成功补货后纳管库存。补货不受销售状态影响，暂停商品仍可补货。
- `stock` → `OrderDesk.stock(sku)`。返回 `{sku, on_hand, reserved, available}`；未纳管商品的 `on_hand`、`available` 为 `null`，`reserved` 为 `0`。
- `place` → `OrderDesk.place(...)`。对已纳管商品按合计数量检查并预留可用量（在库量减预留量），任一商品缺货则整笔订单不创建。清单中只要包含一个暂停销售商品（`enabled` 为 `false`，未纳管商品同样适用），整次抛出 `ValueError`，不返回部分预览、不创建订单或预留库存。
- `amend` → `OrderDesk.amend(order_id, lines)`。替换 `placed` 订单的全部商品行，不另建订单，编号与状态不变。`lines` 结构同 `place`，行内额外字段忽略；商品行保留输入顺序与重复 SKU，单价取提交时商品目录，小计与总金额仍为整数分，`get` 与 `list` 返回修改后的内容。同一 SKU 合并数量检查：新需求不得超过当前可用量加该订单实际预留量；成功后各商品预留量等于其他订单预留加本次需求，在库量不变，移除的商品释放其原预留，此后取消或发货只处理修改后的实际预留。未纳管商品仍不限制数量、不生成预留；下单后才纳管的商品按当前库存检查并预留；无预留记录的旧订单不获得额外额度。对暂停销售商品：允许保留、减量或从清单中移除，但不能加入原清单没有的暂停商品，也不能增加该 SKU 的合计数量；比较时分别合并当前订单行与新清单中的重复 SKU，以当前订购量为准，不使用历史数量或实际预留量（原两行合计三件时重排为三件可通过、四件被拒绝；先减为一件后不能再增至两件）。订单不存在或非 `placed`、编号或 SKU 非法、清单非列表或为空、行非对象或缺少 `sku`/`quantity`、数量非正整数或为布尔值、商品未知、库存不足均抛出 `ValueError`，拒绝不改写数据、不消耗历史序号。每次成功提交（含相同清单重复提交）追加 `action` 为 `amend`、`result` 为修改后订单快照的历史事件，旧事件与 `complete` 不变，无历史的旧订单从 1 开始且 `complete` 为 `false`；修改与历史同次写入 `root/data.json`。
- `quote` → `OrderDesk.quote(lines)`。下单前预览，不创建订单、不预留库存、不写任何文件。`lines` 为非空列表，每行含 `sku`（去除首尾空白后非空字符串，区分大小写）和正整数 `quantity`（不接受布尔值），行内额外字段忽略；清单非列表或为空、行不是对象、缺少必要字段、sku 非法、数量为零/负数/非整数、商品不存在均抛出 `ValueError`，整次预览不返回部分结果。清单中只要包含一个暂停销售商品（含未纳管商品）即整次抛出 `ValueError`。相同 sku 合并数量，商品行按 sku 升序；返回仅含 `lines`、`total_cents`、`can_place`，每行仅含 `sku`、`quantity`、`unit_price_cents`、`subtotal_cents`、`available`、`shortfall`。单价只取当前目录，小计为合并数量乘单价，缺货商品和零价商品都保留。纳管商品 `available` 为在库量减预留量，`shortfall` 为需求超过可用量的部分（未超过为零）；未纳管商品（含无库存字段的旧数据）`available` 为 `null`、`shortfall` 为零。所有行缺口为零时 `can_place` 为 `true`。预览不锁定价格或库存，随后交给 `place` 时仍按当时数据校验和预留。恢复启用后，`quote`、`place`、`amend` 恢复既有规则。
- `get` → `OrderDesk.get(...)`。参数名见 `core.py` 的公开方法签名。
- `save-cart` → `OrderDesk.save_cart(cart_id, lines)`。保存可稍后结算的购物清单，关闭工作台后仍可继续处理。`cart_id` 为去除首尾空白后的非空字符串（区分大小写），与订单编号互不占用；新编号创建清单，同一编号再次保存完整替换。`lines` 为非空列表，每行含 `sku`（规则同 `quote`）和正整数 `quantity`（不接受布尔值），行内额外字段忽略；重复 SKU 合并数量，商品行按 SKU 升序。返回仅含 `cart_id`、`lines`，每行仅含 `sku`、`quantity`。清单非列表或为空、行非对象或缺少 `sku`/`quantity`、SKU 非法、数量非正整数或为布尔值、商品不存在、清单编号非法均抛出 `ValueError`，失败不创建或改写清单。暂停销售和库存不足的商品可以保存；保存不记录价格、不预留库存、不创建订单或历史。清单随业务数据保存在 `root/data.json`，重新打开同一 root 后未结算清单仍可查询。
- `cart` → `OrderDesk.get_cart(cart_id)`。查询已保存的清单，返回仅含 `cart_id`、`lines`（每行仅含 `sku`、`quantity`）。`cart_id` 非法或清单不存在均抛出 `ValueError`；查询不创建目录或文件。旧数据缺少清单时视为空集合，不补造记录。
- `checkout-cart` → `OrderDesk.checkout_cart(cart_id, order_id)`。把已保存清单结算为订单：使用结算当时的目录与库存，遵守 `place` 的商品校验、成交金额和预留规则，成功返回与 `place` 相同的完整订单结果，新增一条 `action` 为 `place` 的历史事件，并同时移除该清单（重新打开后已结算清单不会恢复）。未知清单、编号非法、订单编号已存在、暂停商品或库存不足均抛出 `ValueError`，清单、订单、库存与历史不变。未纳管商品结算仍不限制数量且不产生预留。
- `cancel` → `OrderDesk.cancel(...)`。取消只释放该订单实际预留的数量，在库量不变。
- `ship` → `OrderDesk.ship(order_id, carrier, tracking_no)`。一次性整单发货：状态改为 `shipped`，订单新增仅含 `carrier`、`tracking_no`（均去除首尾空白）的 `shipment`。按该订单实际预留的数量同时扣减在库量与预留量，可用量不变；未纳管商品、下单后才纳管的商品及无预留记录的旧订单不扣库存。订单不存在、已取消或已发货均报错，重复发货不覆盖记录或再次扣减。
- `ship-batch` → `OrderDesk.ship_batch(shipments)`。整批发货登记：一个 `shipments` 请求中的订单全部成功或全部拒绝。`shipments` 为非空列表，每项是含 `order_id`、`carrier`、`tracking_no` 的对象（行内额外字段忽略），三者均为去除首尾空白后的非空字符串，订单编号区分大小写，归一化后重复编号拒绝。列表为空或类型不符、项目不是对象、缺少必要字段、字段非法、编号重复、订单未知或状态不是 `placed`（已取消或已发货）均抛出 `ValueError`，整批不返回部分结果：数据文件内容、库存、预留与历史均不变，也不创建原本不存在的目录。成功时返回按订单编号升序排列的完整订单数组，每项与 `ship` 成功结果相同（状态为 `shipped`，`shipment` 采用去除首尾空白后的值）；每笔订单只扣减其实际预留对应的在库量与预留量，可用量不变，未在本批中的订单预留保留。未纳管商品、下单后才纳管但没有实际预留的商品及无预留记录的旧订单均不扣库存；暂停销售商品仍可发货，订单商品行、金额与重复 SKU 的原有排列保留；不同订单允许使用相同运单号。每笔订单追加一条 `action` 为 `ship`、`result` 为该笔返回快照的历史事件，序号接续该订单原历史，旧事件与已有 `complete` 不变，无历史的旧订单从 1 开始且 `complete` 为 `false`；订单与各自历史同次保存到 `root/data.json`，重新打开结果一致。批量发货后的订单仍按原规则登记退货。命令外层 JSON 数组继续逐项独立执行，前项成功不因后项失败回滚；整批一致性只限单个 `shipments` 请求。
- `record-return` → `OrderDesk.record_return(order_id, return_id, lines)`。分次退货登记，仅接受 `shipped` 订单；只登记退货，不退款、不回补库存，订单状态、金额、商品行、shipment 与其他订单预留量均不变。退货编号在整个 root 内唯一，重复提交（即使内容相同）拒绝且不覆盖。订单编号、退货编号与 SKU 均须为非空字符串（去除首尾空白，区分大小写）；`lines` 为非空列表，每行含 `sku` 和正整数 `quantity`（布尔值不接受）。本次输入及原订单中的重复 SKU 分别合并数量，累计成功退货量加本次数量不得超过原订量，任一 SKU 超量或不属于原订单则整次登记拒绝。成功返回仅含 `order_id`、`return_id`、`lines` 的记录，商品行按 sku 升序，每项仅含 `sku`、`quantity`。
- `returns` → `OrderDesk.get_returns(order_id)`。返回 `{order_id, records, remaining}`：`records` 按 return_id 升序，`remaining` 按 sku 升序覆盖原订单全部商品，数量为原订量减累计退货量，零也保留。已有订单均可查询，无退货记录时 `records` 为空；查询不写文件。
- `receive-return` → `OrderDesk.receive_return(return_id)`。整笔退货入库，与分次退货登记相互独立：登记本身仍不回补库存，入库不要求逐个商品行，数量取该退货编号登记记录中的全部商品行。一次性把各行数量加回当前在库量，预留量及各订单预留记录不变，可用量相应增加。商品必须在当前目录中存在且已纳管（至少成功补货一次）；入库不自动纳管商品，发货后才纳管的商品按当前库存处理。任一商品未知或未纳管则整次入库拒绝。`return_id` 为去除首尾空白后的非空字符串（区分大小写），非字符串、空白编号、未知退货、已撤销编号、所属订单不存在或非 `shipped` 均抛出 `ValueError`。同一 root 内每个退货编号只能成功入库一次，重复提交拒绝且不覆盖原记录或再次加库存；失败不改写数据、不新增历史或消耗序号。成功返回仅含 `order_id`、`return_id`、`lines`，商品行按 sku 升序，每行仅含 `sku`、`quantity`、`before`、`after`；`quantity` 取原退货记录数量（同 sku 合并），`before` 与 `after` 沿用 stock 的完整库存结构（`{sku, on_hand, reserved, available}`）。入库不改变订单状态、金额、商品行、shipment 及退货登记记录。已成功入库的退货不可撤销。
- `cancel-return` → `OrderDesk.cancel_return(return_id)`。撤销一笔尚未入库的整笔退货登记，不接受部分撤销：该笔从 `get_returns` 及 `returns` 的 `records` 中移除，`remaining` 按仍有效的登记重新计算（覆盖原订单全部商品并保留零数量行），其他退货登记不受影响。撤销仅释放可退数量，不增减在库量或预留量，不改变订单状态、金额、商品行与 shipment，也不影响其他订单。成功返回原登记的 `order_id`、`return_id`、`lines`，商品行仅含 `sku` 和 `quantity`，重复 SKU 合并并按 SKU 升序。`return_id` 为去除首尾空白后的非空字符串（区分大小写），非字符串、空白编号、未知退货、重复撤销、已入库以及所属订单不存在或非 `shipped` 均抛出 `ValueError`；`receive-return` 对已撤销编号同样抛出 `ValueError`。失败不改写数据、不新增事件或消耗序号。撤销后原 `return_id` 仍在整个 root 内占用，`record-return` 即使换一笔订单也抛出 `ValueError` 拒绝重用；后续可用新编号登记释放出的可退数量。撤销记录与历史事件随业务变更同次保存到 `root/data.json`，重新打开后结果一致。
- `return-receipt` → `OrderDesk.get_return_receipt(return_id)`。返回入库时的完整快照，仅含 `order_id`、`return_id`、`lines`；后续补货、下单、取消、发货、盘点或再次入库均不改写该快照，重新打开同一 root 结果一致。`return_id` 须为去除首尾空白后的非空字符串（区分大小写），非字符串、空白编号、未知退货及尚未入库均抛出 `ValueError`；查询不创建目录或写文件。无入库记录的旧数据视为尚未入库，不补造记录。
- `return-worklist` → `OrderDesk.return_worklist(stage="pending", order_id=None)`。跨订单的只读退货处理清单，不影响登记、入库、撤销及其他公开入口的既有行为。`stage` 去除首尾空白后只接受 `pending`、`received`、`cancelled`、`all`（非字符串或其他值均抛出 `ValueError`），`all` 表示查询全部阶段；`order_id` 为 `null` 时查询整个 root，否则须为去除首尾空白后区分大小写的非空字符串，未知订单抛出 `ValueError`，已知订单没有匹配登记时返回空数组。有效登记有入库凭据时为 `received`，没有时为 `pending`（无入库凭据的旧数据视为待入库）；撤销登记为 `cancelled`，仍可查询；缺少退货或撤销记录视为空集合，不从历史补造登记。结果为按 `return_id` 升序的数组，每项仅含 `order_id`、`return_id`、`stage`、`lines`、`can_receive`、`blockers`；`lines` 取原登记数量，重复 SKU 合并并按 SKU 升序，每行仅含 `sku`、`quantity`。仅 `pending` 登记检查当前目录和库存：商品不存在时阻碍原因是 `unknown-product`，商品存在但未纳管（含无库存记录的旧数据）时为 `unmanaged`；`blockers` 每项仅含 `sku`、`reason`，按 SKU 升序。`pending` 且无阻碍时 `can_receive` 为 `true`，其余情况（含 `received`、`cancelled` 及有阻碍的 `pending`）为 `false`；非 `pending` 登记的 `blockers` 为 `[]`。暂停销售不阻止入库，不产生阻碍。清单所含登记的订单不存在或非 `shipped` 时，整次查询抛出 `ValueError`，不返回部分结果。登记、入库、撤销或补货后再次查询反映最新阶段与入库阻碍；查询成功或失败均不创建目录或写文件，不改变库存、订单、购物清单、历史及序号；业务数据未变时重新打开相同 root 结果一致。
- `history` → `OrderDesk.history(order_id)`。按实际发生顺序返回订单过程，仅含 `order_id`、`status`、`complete`、`events`：`status` 为订单当前状态；`complete` 表示历史是否包含真实创建事件。`events` 按成功操作先后排列，每项仅含 `sequence`、`action`、`result`；`sequence` 在每个订单内从 1 连续递增，`action` 为 `place`、`amend`、`cancel`、`ship`、`record-return`、`receive-return`、`cancel-return`，`result` 保存对应公开方法成功返回内容的快照（创建保留成交商品行与金额，发货保留 shipment，退货保留归并后的记录，入库保留各行入库前后库存快照，撤销保留原登记的归并记录），后续操作不改写旧快照。操作失败不新增事件、不消耗序号；退货编号即使逆序提交，历史仍按登记顺序显示。历史随业务数据保存在 `root/data.json`，与业务变更同次写入，重新打开同一 root 结果一致。无历史数据的旧订单查询时 `events` 为空、`complete` 为 `false`，不依据状态或已有退货记录补造；这类订单随后成功取消、发货或登记退货时从 1 开始记录新事件，`complete` 仍为 `false`，新创建订单为 `true`。`order_id` 须为去除首尾空白后的非空字符串（区分大小写），非字符串、空白编号及未知订单均抛出 `ValueError`；查询不创建目录或写文件。
- `count-stock` → `OrderDesk.count_stock(count_id, lines)`。库存盘点登记，用实际清点数量校准已纳管商品的在库量。`count_id` 为去除首尾空白后的非空字符串（区分大小写），在 root 内唯一，重复提交（即使内容相同）拒绝且不覆盖。`lines` 为非空列表，每行含 `sku`（去除首尾空白后非空字符串，区分大小写）和非负整数 `on_hand`（含零，不接受布尔值），行内额外字段忽略；同一盘点中重复 sku 拒绝，不合并数量。每个商品必须存在且已纳管（至少成功补货一次），新在库量不得低于当前预留量；任一商品未知、未纳管或低于预留量则整次盘点拒绝。成功后一次性替换本次全部商品的在库量，预留量及各订单实际预留记录保留，未列出商品的在库量与预留量均不变。返回仅含 `count_id` 和 `lines`，商品行按 sku 升序，每行仅含 `sku`、`before`、`after`、`delta`；`before` 与 `after` 沿用 stock 的完整库存结构（`{sku, on_hand, reserved, available}`），`delta` 为盘点后在库量减盘点前在库量。盘点记录与库存变更同次写入 `root/data.json`。
- `stock-count` → `OrderDesk.get_stock_count(count_id)`。返回登记时的盘点快照，仅含 `count_id` 和 `lines`；后续补货、下单、取消、发货或再次盘点均不改写该快照，重新打开同一 root 结果一致。`count_id` 须为去除首尾空白后的非空字符串（区分大小写），非字符串、空白编号及不存在的编号均抛出 `ValueError`；查询不创建目录或写文件。无盘点记录的旧数据可正常使用，不补造记录。
- `stock-history` → `OrderDesk.stock_history(sku)`。按商品追溯在库量与预留量的变动，仅返回 `{sku, complete, events}`：`complete` 表示历史是否覆盖首次纳管以来的全部变动。`events` 按成功变动先后排列，`sequence` 在该商品内从 1 连续递增，每项仅含 `sequence`、`action`、`reference_id`、`before`、`after`；`before` 与 `after` 沿用 stock 的完整库存结构（`{sku, on_hand, reserved, available}`）。补货、下单、改单、取消、发货、退货入库和盘点在成功改变在库量或预留量时各记录一条事件，`action` 为对应单笔命令名（`restock`、`place`、`amend`、`cancel`、`ship`、`receive-return`、`count-stock`），购物清单结算记为 `place`，批量发货记为 `ship`；`reference_id` 在补货时为 `null`，下单、改单、取消、发货时为订单编号，退货入库和盘点时为退货编号和盘点编号。批量发货按请求内订单顺序逐笔记录，共用商品的快照连续衔接；改单每个 SKU 仅记录最终净变化，不记录临时释放与预留；同值盘点、净库存不变的改单及没有实际预留的取消或发货均不新增事件，既有业务结果与订单历史照常产生。首次补货从未纳管变为纳管也算变化，其 `before` 为 stock 的未纳管结果；暂停销售不影响记录规则。事件与业务变更同次保存在 `root/data.json`，重新打开结果一致，后续操作不改写旧快照；操作失败不新增事件或消耗序号，整批发货拒绝时也不留下事件。尚未纳管的商品返回 `complete` 为 `true` 的空历史；已纳管但缺少历史的旧商品返回 `complete` 为 `false` 和空事件，后续事件从 1 开始且 `complete` 一直为 `false`，不从已有订单或盘点补造过程。`sku` 去除首尾空白后匹配并区分大小写，非字符串、空白值或未知商品均抛出 `ValueError`；查询不创建目录或写文件。
- `pick-list` → `OrderDesk.pick_list(order_ids)`。只读拣货汇总，合并所选待发货订单的备货数量并保留各订单数量明细，仅供备货参考，不影响 `ship` 及其他公开入口的既有行为。`order_ids` 为非空列表，每项是去除首尾空白后的非空字符串（区分大小写），归一化后重复编号不接受；列表类型不符、编号非法、订单不存在或状态不是 `placed` 时整次查询抛出 `ValueError`，不返回部分汇总。返回仅含 `order_ids`（所选编号升序）和 `lines`（按 sku 升序），每行仅含 `sku`、`quantity`、`reserved`、`available`、`shortfall`、`orders`：`quantity` 为所选订单当前商品行数量合计（订单内重复 SKU 也合并）；`orders` 按 order_id 升序列出包含该商品的订单，每项仅含 `order_id`、`quantity`（该订单合并需求量）、`reserved`（该订单实际预留量）；行级 `reserved` 只合计所选订单的实际预留量，不计入其他订单预留，也不把标称需求当成预留。纳管商品 `available` 与 stock 一致，`shortfall` 为 `quantity - reserved - available` 与零取较大值（可用量为零但自身已足额预留时缺口仍为零）；未纳管商品（含缺少库存记录的旧数据）`available` 为 `null`、`reserved` 和 `shortfall` 为零，商品行仍保留。缺少订单预留记录时实际预留量为零；下单后才纳管的商品按当前可用量计算缺口，不补造预留。暂停销售商品仍参与汇总，`amend` 后再次查询以修改后的数量为准。查询不创建目录或文件，不改变库存、订单、购物清单、历史事件及序号；业务数据未变时重新打开同一 root 结果一致。
- `list` → `OrderDesk.list_orders(...)`。参数名见 `core.py` 的公开方法签名。

命令成功向标准输出打印 JSON 并返回 0；输入或本地文件错误向标准错误输出说明并返回 2。无参数的方法可省略输入文件。数据保存在 `root/data.json`，每次成功修改后保存；适用于单进程本地使用。

## 样例

`examples/` 提供 3 份虚构业务样例。`tests/` 覆盖业务路径、拒绝非法操作后的状态和命令入口。

## 当前边界

当前仅支持一币种、一个商品目录、一次性取消、逐单与整批发货、分次退货登记（不退款、不回补库存）、整笔退货入库（不退款、不分批入库）、撤销尚未入库的退货登记（整笔撤销、不接受部分撤销）、跨订单只读退货处理清单、按商品的库存预留、库存盘点登记及按商品追溯库存变动。没有支付、拆单或物流联网功能。 不承诺并发写入或断电恢复。
