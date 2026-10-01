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
- `list` → `OrderDesk.list_orders(...)`。参数名见 `core.py` 的公开方法签名。

命令成功向标准输出打印 JSON 并返回 0；输入或本地文件错误向标准错误输出说明并返回 2。无参数的方法可省略输入文件。数据保存在 `root/data.json`，每次成功修改后保存；适用于单进程本地使用。

## 样例

`examples/` 提供 3 份虚构业务样例。`tests/` 覆盖业务路径、拒绝非法操作后的状态和命令入口。

## 当前边界

当前仅支持一币种、一个商品目录、一次性取消和按商品的库存预留。没有支付、发货、退货功能。 不承诺并发写入或断电恢复。
