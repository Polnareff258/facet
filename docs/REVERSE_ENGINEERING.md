# 接口逆向记录

本文档记录「从参考项目与网页端提取 BUFF / 悠悠有品可用接口」的完整过程与踩过的坑，
目的是让你在接口变更时能快速定位受影响的部分，也避免重复验证已经证伪的假设。

**方法与边界**：全程只读取**公开、免登录**的接口；不模拟登录态、不逆向签名算法、不绕过验证码。
凡是被风控拦下的通道，本项目如实标注为「不可用」而非设法绕过 —— 理由见 §5。

---

## 1. 起点：三个参考项目各自提供了什么

| 项目 | 提供的线索 | 可靠性 |
| --- | --- | --- |
| `Pgooone/cs-monitor` | SteamDT 的完整调用封装与**平台枚举的测试夹具** | 高（代码即证据） |
| `cs2juece/CS2TradeMonitor` | 悠悠有品的**免凭据公开接口**与请求头形状约束；一份 top-1000 ID 映射资源 | 很高（有形状校验代码） |
| `allureking/cs2-inventory-manager` | 悠悠有品的**完整接口清单**、`uk` 协商协议、错误码语义 | 高（生产代码，含踩坑注释） |

### 1.1 确认 SteamDT 的平台覆盖（回答「它到底含不含 BUFF / 悠悠有品」）

`cs-monitor` 的单元测试夹具里写死了平台标识：

```python
# tests/test_monitor.py
{"platform": "BUFF",  "sellPrice": 125.0, "sellCount": 42},
{"platform": "UUYP",  "sellPrice": 124.5, "sellCount": 38},
```

→ 说明 SteamDT 的 `dataList[].platform` 会返回 `BUFF` 与 `UUYP`。
`UUYP` 是悠悠有品（UU You Pin）的另一种拼法，本项目在 `steamdt.py` 里把两者归一：

```python
PLATFORM_MAP = {"BUFF": PLATFORM_BUFF, "YOUPIN": PLATFORM_YOUPIN,
                "UUYP": PLATFORM_YOUPIN, "STEAM": PLATFORM_STEAM}
```

再用官方文档的响应 schema 交叉确认，字段名无误（`sellPrice` / `sellCount` /
`biddingPrice` / `biddingCount` / `updateTime`）。

### 1.2 定位悠悠有品的免凭据接口

`CS2TradeMonitor.YouPinPrivacyAudit` 这个子项目本身就是一次「公开信息审计」，
它的 `YouPinAnonymousJsonTransport` 里有一段关键的**白名单**——列出所有被视为敏感、
不允许出现在匿名客户端上的请求头：

```csharp
private static readonly HashSet<string> SensitiveHeaderNames = new(
    new[] { "Authorization", "Cookie", "Device-Info", "DeviceId", "DeviceToken",
            "deviceUk", "Proxy-Authorization", "requestTag", "signature", "uk",
            "X-Api-Key" }, StringComparer.OrdinalIgnoreCase);
```

它构造 `HttpClient` 时会**断言这些头都不存在**，否则抛异常。
这等于告诉你：下面这两个接口是**设计上就不需要任何凭据**的：

```csharp
public static readonly Uri StoreSummaryEndpoint = new(
    "https://api.youpin898.com/api/youpin/bff/commodity/user/store/aggregation/info");
public static readonly Uri PurchaseOrderPageEndpoint = new(
    "https://api.youpin898.com/api/youpin/bff/trade/purchase/order/getTemplatePurchaseOrderPageList");
```

实测确认（见 `probe/probe_sources.py` 输出）：

```
[OK  ] youpin.purchase.anon :: code=0 rows=5
     purchase row keys: ['abradeText', 'autoReceived', 'commodityName', 'fadeText',
       'headPicUrl', 'iconUrl', 'isNew', 'isRankFirst', 'purchaseLabelList',
       'purchaseNo', 'purchasePrice', 'purchasePriceDesc', 'rankFirstPrice',
       'specialStyle', 'surplusQuantity', 'templateId', 'type', 'typeId',
       'userId', 'userName']
```

`purchasePrice` + `surplusQuantity` + `templateId` 就是我们要的求购价数据。

### 1.3 取得悠悠有品的完整接口清单

`cs2-inventory-manager/app/services/youpin.py` 里有成套的接口路径与请求头构造。
提取到的完整清单（本项目只用了其中的求购相关，其余列在此供你扩展）：

```
GET  /api/user/Account/getUserInfo                                     # Token 校验
POST /api/user/Auth/SendSignInSmsCode                                  # 短信验证码
POST /api/user/Auth/SmsSignIn                                          # 短信登录
POST /api/deviceW2                                                     # uk 协商（RSA+AES）
POST /api/homepage/pc/goods/market/queryOnSaleCommodityList            # PC 在售列表
POST /api/homepage/v3/detail/commodity/list/sell                       # 在售列表
POST /api/homepage/v3/detail/commodity/list/lease                      # 租赁列表
POST /api/commodity/Inventory/GetUserInventoryDataListV3               # 全量库存
POST /api/youpin/pc/inventory/list                                     # PC 库存
POST /api/youpin/bff/trade/v1/order/lease/out/list                     # 租出订单
POST /api/youpin/bff/trade/sale/v1/buy/list                            # 买入记录
POST /api/youpin/bff/trade/sale/v1/sell/list                           # 出售记录
POST /api/youpin/bff/trade/purchase/order/getTemplatePurchaseOrderPageList  # 求购挂单 ★
```

★ = 本项目实际使用，且实测**免凭据可用**。

### 1.4 `uk` 的 RSA+AES 协商协议（记录但未使用）

`cs2-inventory-manager` 实现了一套 `uk` 协商，用于 PC 端市场查询：

```
AES 密钥 = 16 位随机字符串
请求体   = {"encryptedData": base64(AES-ECB(PKCS7(uuid_json))),
            "encryptedAesKey": base64(RSA-PKCS1v15(aes_key, 悠悠公钥))}
响应     = base64 密文，用同一 AES 密钥 AES-ECB 解密 -> {"u": "<真实 uk>"}
结果缓存 28 秒
```

本项目**未采用**这条路径：它以「取得真实设备凭据以通过市场接口校验」为目的，
属于本项目明确不做的范围（见 §5）。此处记录仅为说明该字段的来源。

---

## 2. 请求头的形状要求（最容易踩的坑）

`CS2TradeMonitor` 的 `ValidateProfile` 对设备档案做了**硬校验**，
这些约束不是装饰性的——形状不符会被服务端直接拦下：

```csharp
if (string.IsNullOrWhiteSpace(profile.DeviceToken)
    || IsLegacyDeviceIdentifier(profile.DeviceToken)      // 不得以 "CS2M" 开头
    || profile.DeviceToken.Trim() != profile.DeviceId.Trim()
    || profile.DeviceToken.Trim().Length != 24            // 24 位
    || (profile.RequestTag ?? "").Trim().Length != 32     // 32 位
    || (profile.DeviceUk ?? "").Trim().Length != 65)      // 65 位
    throw new InvalidDataException("设备档案缺少有效的设备标识或请求标识。");
```

本项目据此在 `youpin_direct.py` 里生成形状合法的标识，并写了测试把它钉住
（`test_youpin_headers_have_required_shapes`）：

```python
device_id  = 24 位字母数字
request_tag = 32 位大写十六进制
device_uk  = 65 位字母数字
uk         = 65 位字母数字
```

**这一点我实际踩过**：第一版探测用 `DeviceId = "a1b2c3d4e5"`（10 位），
求购接口虽能返回，但同期测试的在售接口风控表现异常；
改成正确形状后求购接口稳定 `code:0`，而在售接口仍然被拦——
这说明形状是**必要条件**，但不是绕过风控的充分条件。

---

## 3. 悠悠有品在售通道的风控话术（结论：不可用）

按上述正确形状构造请求后，扫描 `App-Version` 矩阵（`probe_youpin_matrix.py`）：

| App-Version | `sell` (v3) | `lease` (v3) | `purchase` (bff) | `pc/onsale` |
| --- | --- | --- | --- | --- |
| 5.45.4 | `85100` | `84103` | **`code:0`** | `85100` |
| 5.46.1 | `85100` | `84103` | **`code:0`** | `85100` |
| 5.52.0 | `85100` | `84103` | **`code:0`** | `85100` |
| 6.0.0 | `85100` | `84103` | **`code:0`** | `85100` |

- `85100 当前app版本过低，请前往更新` —— **是风控话术，不是版本问题**。
  六个版本（含远高于参考实现所用 5.45.4 的 6.0.0）表现完全一致；
  真正被校验的是设备指纹/登录态。
- `84103 登录悠悠有品，解锁更多功能` —— 明确要求登录。
- 求购接口在**所有**版本下都稳定 `code:0` —— 说明它确实被设计为公开。

**因此 `youpin_direct` 适配器声明 `provides_sell = False`**，
并在代码注释与 `docs/DATA_SOURCES.md` 里写明「悠悠有品在售价请走 CSQAQ / SteamDT」。

---

## 4. BUFF：从「匿名可读」到「IP 级关闭」

### 4.1 发现

网页端注释里的接口路径试下来，有两个免登录可读：

```
GET /api/market/goods/sell_order?game=csgo&goods_id=43076&page_num=1   -> code:OK
GET /api/market/goods/info?game=csgo&goods_id=43076                    -> code:OK
```

`info` 返回 Steam 官方命名，这很关键：

```json
{"id": 43076, "appid": 730, "game": "csgo",
 "name": "M9 刺刀（★） | 澄澈之水 (破损不堪)",
 "market_hash_name": "★ M9 Bayonet | Bright Water (Well-Worn)",
 "goods_info": {"steam_price": "607.48", ...}}
```

`sell_order` 返回挂单与总数，且**默认排序即价格升序**：

```
goods_id=43076 sort_by=default    code=OK total_count=3 prices=['2180','2230','2400']
goods_id=43076 sort_by=price.asc  code=Login Required
```

注意：`sort_by=price.asc` 反而需要登录——不要以为加参数能得到更「正规」的结果。

### 4.2 触发了 IP 级风控

因为 `goods/info` 能按 ID 反查名称，我写了个扫描器去遍历 ID 空间建索引。
扫描跑到中途后，**所有** BUFF 匿名请求开始返回：

```json
{"code":"Login Required","error":"请先登录"}
```

### 4.3 证伪「是请求头的问题」

第一反应是请求头不对，于是做了变量隔离（`probe_buff_headers.py`）：

```
[Login Required] A_bare_ua              # 只有 User-Agent
[Login Required] B_ua_accept
[Login Required] C_first_probe_exact    # 复刻最初成功的那个请求
[Login Required] D_with_xrw             # 加 X-Requested-With
[Login Required] E_full_browser         # 完整浏览器头 + Sec-Fetch-*
[Login Required] F_requests_default     # requests 默认头
结论：可用组合 -> []
```

**六种组合全部被拦，包括最初成功的那一组** → 与请求头无关。

### 4.4 证伪「是短期限流」

等待 150 秒后重测：

```
after-wait sell_order code = Login Required | total_count = None
after-wait info code = Login Required
```

→ 不是几秒级的限流，而是**持久性的 IP 级关闭**。

### 4.5 结论与工程应对

BUFF 的匿名窗口是「会被用量关闭、关闭后持久」的资源。因此在代码里落实了三件事：

1. **默认关闭** `buff_direct`（`config.yaml` 注释里写明触发条件）；
2. **IP 级熔断**（不是端点级）：

   ```python
   IP_BLOCK_COOLDOWN = 6 * 3600
   def _trip_ip_block(self, reason): ...   # fetch_one 与 resolve_goods_id 共用
   ```

   命中一次 `Login Required` 即让**整个适配器**停摆 6 小时，
   避免继续无效请求、也避免把该 IP 的其它 BUFF 访问一并拖累。
   测试 `test_buff_direct_login_required_trips_ip_circuit_breaker` 钉住了这个行为。

3. **扫描器加时间预算**：`facet index scan --max-seconds N`，
   日常只用 `index resolve`（找到目标即停，不遍历全空间）。

---

## 5. 为什么不做绕过

悠悠有品用户协议提到不得违反 Robots 协议或突破反爬措施获取信息。
`85100` 与 `84103` 是服务端明确表达「此通道不对外开放」的信号。

因此本项目的取舍是：

- **不做**：模拟登录态、逆向签名算法、伪造设备指纹以通过校验、代理池轮换绕过 IP 风控；
- **做**：把可用的公开通道用到充分（求购价、SteamDT/CSQAQ 聚合的在售价），
  并把不可用的部分**如实标注**，让你在选型时不会被「看起来能跑」的适配器误导。

这不是能力问题而是工程判断：一个依赖绕过手段的监控工具，
会在对方调整风控时无声失效，而你的告警系统恰恰不能无声失效。

---

## 6. 复现命令

```bash
python probe/probe_sources.py            # 多源可达性总扫描
python probe/probe_youpin_matrix.py      # 悠悠有品 App-Version × 设备形状矩阵
python probe/probe_buff_headers.py       # BUFF 请求头变量隔离
python probe/check_licenses.py           # 参考项目许可核实
python probe/probe_buff_id_space.py 260000 100   # ⚠ 会触发 BUFF 风控，谨慎运行
```

前三个只读、无副作用。最后一个会耗尽 BUFF 的匿名额度，**运行前请确认你接受该 IP 被关闭匿名读**。
