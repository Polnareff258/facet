# 数据源评估矩阵（含实测证据）

本文档记录每个候选数据源的**接口、字段、限额、平台覆盖**以及**实测结论**。
所有「实测」标注均来自本项目在 2026-09 的实际请求，探测脚本保留在 `probe/` 下可复现。

> 结论速览：**CSQAQ 是唯一能一次调用同时给出 BUFF + 悠悠有品在售价的源，应作为主力；
> SteamDT 作为低频兜底并补求购价；两个免登录直连源是机会型补充，且各有硬边界。**

---

## 1. 覆盖矩阵

| 平台 | CSQAQ | SteamDT | BUFF 直连 | 悠悠有品直连 |
| --- | --- | --- | --- | --- |
| BUFF 在售价 | ✅ `buffSellPrice` | ✅ `platform=BUFF` | ✅ 条件可用（见 §4） | — |
| BUFF 在售量 | ✅ `buffSellNum` | ✅ `sellCount` | ✅ `total_count` | — |
| BUFF 求购价 | ❌ | ✅ `biddingPrice` | ❌（`bill_order` 需登录） | — |
| 悠悠有品 在售价 | ✅ `yyypSellPrice` | ✅ `platform=YOUPIN/UUYP` | — | ❌ 被风控（见 §5） |
| 悠悠有品 在售量 | ✅ `yyypSellNum` | ✅ `sellCount` | — | — |
| 悠悠有品 求购价 | ❌ | ✅ `biddingPrice` | — | ✅ 免凭据可用 |
| Steam 在售价 | ✅ `steamSellPrice` | ✅ `platform=STEAM` | — | — |

**关键回答：**
- 「SteamDT 是否覆盖 BUFF、悠悠有品？」→ **是**。批量接口返回的 `dataList[].platform`
  枚举实测含 `BUFF`、`YOUPIN`（部分接口拼作 `UUYP`，本项目归一为同一平台）。
- 「CSQAQ 是否覆盖这两个平台？」→ **是**，且一次调用同时返回，字段名带平台前缀，语义清晰。

---

## 2. CSQAQ 数据开放 API（主力源）

### 接口

```
POST https://api.csqaq.com/api/v1/goods/getPriceByMarketHashName
Header: ApiToken: <YOUR_API_TOKEN>
Body:   {"marketHashNameList": ["★ Bowie Knife", "AWP | Snake Camo (Factory New)"]}
```

- 单次 `marketHashNameList` 长度 **≤ 50**
- 限额：**不限次，单 IP 1 次/秒**
- 鉴权：Token 注册即得，但**必须在官网绑定本机白名单 IP**，否则 401/400

### 响应结构（文档示例，节选）

```json
{
  "code": 200,
  "msg": "Success",
  "data": {
    "success": {
      "★ Bowie Knife": {
        "goodId": 6733, "name": "鲍伊猎刀（★）",
        "marketHashName": "★ Bowie Knife",
        "buffSellPrice": 1340.0, "buffSellNum": 43,
        "yyypSellPrice": 1309.0, "yyypSellNum": 35,
        "steamSellPrice": 1947.77, "steamSellNum": 11
      }
    },
    "error": ["test_name"]
  }
}
```

要点：
- `data.error` 列出未能识别的名称 —— **是正常返回，不是报错**，适配器只记日志不中断。
- `goodId` 是 **CSQAQ 自己的 ID 空间**，经实测**不等于** BUFF 的 `goods_id`（见 §6 的证伪记录），
  不要拿它去调 BUFF 接口。
- 错误码：`400` 用户不存在或 Token 失效 / `401` Token 未通过 / `429` 请求过多 / `503` 网关异常。

### 本项目实现

`facet/sources/csqaq.py` —— `batch_size=50`、`min_interval=1.05s`（略高于 1 秒留抖动余量）。

---

## 3. SteamDT 开放平台（补充源）

### 接口

```
POST https://open.steamdt.com/open/cs2/v1/price/batch
Header: Authorization: Bearer <API_KEY>
Body:   {"marketHashNames": ["..."]}          # 1..100
```

响应（文档 schema + 实测字段名）：

```json
{
  "success": true,
  "data": [
    {
      "marketHashName": "AK-47 | Redline (Field-Tested)",
      "dataList": [
        {"platform": "BUFF",  "platformItemId": "...", "sellPrice": 125.0, "sellCount": 42,
         "biddingPrice": 118.0, "biddingCount": 30, "updateTime": 1700000000000}
      ]
    }
  ],
  "errorCode": 0, "errorMsg": "", "errorData": {}, "errorCodeStr": ""
}
```

- `updateTime` 是 **epoch 毫秒**（适配器同时兼容秒级时间戳）
- 限额：**批量 1 次/分钟**；单件 60 次/分钟；`get_all_items` 基础信息 **每天 1 次**
- 限流表现：HTTP 429，或 HTTP 200 但 `success=false` 且 `errorCode=4029` / 文案含「限流/频繁」
  —— 两种都走冷却处理

### 其它可用接口（本项目暂未接入）

| 接口 | 用途 |
| --- | --- |
| `POST /open/cs2/item/v1/kline` | 饰品 K 线（`type` 1=时K 2=日K 3=周K，`platform` 可选） |
| `GET /open/cs2/v1/price/avg` | 同饰品**所有平台近 7 天均价**（可作基准价交叉验证） |
| `GET /open/cs2/v1/base` | 全量饰品基础信息（**每天 1 次**，必须本地缓存） |
| `POST /open/cs2/v1/price/single` | 单件查询（60 次/分钟） |

调试建议：官方提供面向 LLM 的索引文档 `https://doc.steamdt.com/llms.txt`，比逐个翻页面快得多。
本项目已抓取一份放在 `docs_steamdt_llms.txt`（探测产物，不入库）。

### 本项目实现

`facet/sources/steamdt.py` —— `batch_size=100`、`min_interval=60.5s`。
因为是 1 次/分钟级别的限额，**默认不启用**：让 1 分钟只能取 100 个饰品的源去做高频轮询不划算，
它的正确定位是「低价兜底 + 求购价来源」。

---

## 4. BUFF 数据源（两种模式，能力差很多）

### 可用性矩阵（实测，`probe/probe_buff_capability.py` 可复现）

| 接口 | 匿名 | 带 Cookie | 用途 |
| --- | --- | --- | --- |
| `GET /api/market/goods/sell_order` | ✅ | ✅ | **在售最低价 + 在售量** |
| `GET /api/market/goods/buy_order` | ✅ | ✅ | **最高求购价 + 求购量** |
| `GET /api/market/goods/info` | ✅ | ✅ | goods_id → market_hash_name |
| `sell_order?paintseed=N` | ❌ Login Required | ✅ | 档位级在售价 |
| `sell_order?min_paintwear=N` | ❌ Login Required | ✅ | 磨损区间筛选 |
| `sell_order?sort_by=...` | ❌ Login Required | ✅ | 自定义排序 |
| `GET /api/market/goods`（列表/搜索） | ❌ Login Required | ✅ | **按名称解析 goods_id** |
| `GET /api/market/goods/bill_order` | ❌ Login Required | ✅ | 成交记录 |

**新增发现：`buy_order` 匿名可用。** 这补上了 BUFF 侧此前缺失的「能立刻卖出多少钱」：

```json
GET /api/market/goods/buy_order?game=csgo&goods_id=43076&page_num=1
→ {"code":"OK","data":{"total_count":3,"items":[
     {"price":"1970","num":1,"state":"progressing","pay_method_text":"先求后付"},
     {"price":"1960",...},{"price":"1900",...}]}}
```

返回按价格降序（1970 > 1960 > 1900），`items[0].price` 即最高求购价。
实测 `goods_id=43076`：在售价 2180 / 在售量 3 / **求购价 1970** / 求购量 3。

### 配 Cookie 的真正价值：用搜索替代扫描

不带 Cookie 时，`goods` 列表接口也是 `Login Required`，意味着**无法按名称找 goods_id** ——
早期只能靠扫描 ID 空间碰运气，而那正是触发 IP 级风控的操作。

配了 Cookie 后 `search_goods()` 可用，加一个监控只需一次精确查询：

```python
adapter.resolve_by_name("AK-47 | Redline (Field-Tested)")
# → {"goods_id": 46682, "market_hash_name": ..., "sell_num": 1234, ...}
```

### ⚠ 匿名额度的硬边界（未变）

即使匿名可用的接口，也会被**用量触发的 IP 级风控**整体关闭。实测扫描 ID 空间后
所有匿名请求转为 `Login Required`，与请求头无关（六种头组合全部被拦），
等待 150 秒不恢复。因此：

1. `buff` 源**默认关闭**（`config.yaml`）；
2. 适配器内置 **6 小时 IP 级熔断**（`BuffDirectAdapter.IP_BLOCK_COOLDOWN`）；
3. **强烈建议配 Cookie** —— 有了搜索就不需要扫描，触发风控的概率大幅下降。

获取方式见 `.env.example` 或直接跑 `python -m facet setup buff`（会引导抓取并当场验证）。

### 能力自检

```bash
python -m facet setup buff        # 交互式配置 + 验证
python -m facet doctor            # 看 BUFF 当前是匿名模式还是 Cookie 模式
python -m facet sources           # 看源的启用与凭证状态
```

代码里也可随时查询：`adapter.capabilities()` 返回当前真实可用的能力布尔表，
刻意不做「假装支持」——缺什么就报什么。

---

## 5. 悠悠有品（匿名只有求购价）

### 可用（实测 `code:0`）

```
POST https://api.youpin898.com/api/youpin/bff/trade/purchase/order/getTemplatePurchaseOrderPageList
Body: {"pageIndex":1,"pageSize":20,"showMaxPriceFlag":false,"templateId":822}
```

返回 `data.responseList[]`，字段含：

```
purchasePrice, purchasePriceDesc, surplusQuantity, templateId,
commodityName, userId, userName, isRankFirst, rankFirstPrice,
iconUrl, abradeText, fadeText, specialStyle, autoReceived
```

- **完全免凭据**（不需要 `uk`、不需要 `authorization`、不需要签名），实测多次返回 `code:0`
- 实测样例：`templateId=822` → 最高求购出价 **¥60.00**

其它两个免凭据可用的公开接口（非行情，但同一命名空间下确实开放）：

```
POST /api/youpin/bff/commodity/user/store/aggregation/info
Body: {"userId":"1","pageType":"user_store"}     -> 店铺聚合信息，code:0
```

### 不可用（匿名请求被风控拦下）

| 接口 | 匿名返回 |
| --- | --- |
| `POST /api/homepage/v3/detail/commodity/list/sell` | `85100 当前app版本过低，请前往更新` |
| `POST /api/homepage/pc/goods/market/queryOnSaleCommodityList` | `85100 同上` |
| `POST /api/homepage/v3/detail/commodity/list/lease` | `84103 登录悠悠有品，解锁更多功能` |

**`85100`「版本过低」是风控话术，不是版本问题**：本项目扫过
`5.28.3 / 5.42.0 / 5.45.4 / 5.46.1 / 5.52.0 / 6.0.0` 六个 `App-Version` 值，
并按参考实现的形状要求构造了完整的设备标识（`DeviceId`/`DeviceToken` 24 位、
`requestTag` 32 位大写十六进制、`deviceUk`/`uk` 65 位），**全部被 85100 拒绝**。

### 授权与合规

悠悠有品用户协议提到不得违反 Robots 协议或突破反爬措施获取信息。因此：

- 本项目**不实现**在售/租赁行情的绕过方案（不模拟登录态、不逆向签名）；
- `youpin_direct` 只使用上述**公开免凭据**的求购接口，并如实声明 `provides_sell = False`；
- 悠悠有品的**在售价/在售量**请通过 **CSQAQ / SteamDT** 获取（它们从悠悠有品取数并对外提供）。

---

## 6. 被证伪的假设（记录下来避免重复踩）

**假设：CSQAQ 的 `goodId` 就是 BUFF 的 `goods_id`。**
若成立，就能省掉 BUFF 索引扫描。实测证伪：

| CSQAQ `goodId` | CSQAQ 声称的饰品 | BUFF 同 ID 实际返回 |
| --- | --- | --- |
| 6733 | ★ Bowie Knife | `Inscribed Ice Burst Bow`（DOTA2 饰品） |
| 7189 | ★ Huntsman Knife \| Tiger Tooth (FN) | `Inscribed Greatsword of the Cyclopean Marauder` |
| 301 | AWP \| Snake Camo (Factory New) | `Wardfish` |

命中 **0/3**。同时确认 BUFF 的 `goods_id` 是**跨游戏共享**的 ID 空间
（`1` = DOTA2 的 `Warhound of the Chaos Wastes`，`43076` = CS2 的 M9 刺刀），
因此不能假定「CS2 的 ID 都在某一小段」。

**另一个观察：悠悠有品的 `templateId` 与 BUFF 的 `goods_id` 也是两套独立空间。**
实测同一数值 43076：BUFF 上是 `★ M9 Bayonet | Bright Water (Well-Worn)`，
悠悠有品上是「封装的涂鸦 | 爪子刀 (豆青)」。**绝不可跨平台混用 ID。**

---

## 7. 身份映射的获取途径（本项目 `facet/mapping.py`）

因为三个平台的 ID 互不相通，映射层是必须的。按可靠性排序：

1. **显式声明** —— CLI `--buff-id` / `--youpin-id`，或 `config.yaml` 的 watchlist 项。最可靠。
2. **社区沉淀的映射表** —— 参考项目 CS2TradeMonitor 随包附带 `hot-top1000.youpin-mapping.json.gz`，
   结构为 `{marketHashName: {steam_hash_name, yyyp_id}}`，可直接导入：
   ```bash
   python -m facet seed
   ```
3. **BUFF ID 空间扫描** —— `facet/indexer.py`，只产出 BUFF 侧，且**有风控风险**（见 §4）。

已解析率可随时查：`python -m facet index status` → `映射覆盖率`。

---

## 8. 探测脚本

`probe/` 下的脚本可复现上述全部结论（只读探测，不写业务数据）：

| 脚本 | 用途 |
| --- | --- |
| `probe_sources.py` | 多源可达性总扫描（BUFF 匿名面 / 悠悠有品在售通道 / 加密库可用性） |
| `probe_youpin_matrix.py` | 悠悠有品 App-Version × 设备标识形状矩阵 |
| `probe_buff_headers.py` | BUFF 请求头变量隔离（用于证伪「是请求头的问题」） |
| `probe_buff_id_space.py` | BUFF goods_id 空间分布扫描（**会触发风控，谨慎运行**） |
