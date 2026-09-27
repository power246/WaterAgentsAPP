# M4 参数能力目录

更新日期：2026-09-25

> 状态：第一版集成交付目标，尚未实现。本文列出 M1–M9 完成后进入正式前后端联调前必须接通的 154 个参数能力；它不是当前代码已实现能力的声明，也不是把 USGS 全目录直接当作 allowlist。

本文承接：

- [M4 设计 D09/D22](m4_query_understanding_topology_design.md#d09完整参数目录用于发现能力注册表决定能否执行)
- [M4 实施设计 §8](m4_query_understanding_topology_implementation_design.md#8-参数能力注册表)
- [M4 Implementation TODO Milestone 2](m4_implementation_todo.md#milestone-2154-项能力注册表与参数解析)
- [跨 Milestone M4-C04](../deferred_milestone_contracts.md#6-m4query-understanding--hydrology-topology)

## 1. 调研来源与口径

本目录在 2026-09-25 依据 USGS 现代 Water Data API 调研并逐项查询：

- [Parameter Codes v1](https://api.waterdata.usgs.gov/ogcapi/v1/collections/parameter-codes/items?f=html) 提供 5 位代码、名称、单位、分组、介质和样品 fraction；字段语义见 [parameter-codes schema](https://api.waterdata.usgs.gov/ogcapi/v1/collections/parameter-codes/schema?f=html)。
- [time-series-metadata](https://api.waterdata.usgs.gov/ogcapi/v1/collections/time-series-metadata?f=html) 用于判断 continuous/daily 序列及其 period of record；水质离散样品的 period of record 按 USGS [modernized API migration guide](https://api.waterdata.usgs.gov/docs/ogcapi/migration/) 使用 Samples API `/summary`，真实观测再从对应数据端点读取。
- 本文件的 154 个代码在调研快照中均从官方 `parameter-codes` v1 精确返回。该结果只证明身份和官方单位存在，不证明任意地点、任意日期都有观测，更不证明项目 adapter、单位换算和推理规则已经实现。

USGS 完整目录用于候选发现；下面的有限清单是项目第一版的交付范围。新增或删除基线条目必须同时更新本文、唯一 checked-in manifest、契约测试和用户可见能力说明，不能只改 prompt。

## 2. “可执行能力”的完成门禁

每一行只有同时满足以下条件，production manifest 才能标记 `enabled=true`：

1. `ParameterKey=usgs:<code>`、规范名称、canonical unit、中文/英文 aliases 和介质/fraction 语义明确；
2. availability provider 能按 COMID/站点和请求时间检查连续序列或样品记录，技术失败不会伪装成空结果；
3. observation provider 能读取真实记录，并有单位转换、质量标志、缺失值和时间聚合规则；
4. direct/indirect evidence 关系已登记；没有证据时按 M4 D05–D06 停止对应推理，不让 LLM 临时发明关系；
5. 至少有 provider contract fixture、单位测试、availability 边界测试和一条解析 alias 测试。

正式联调门禁要求 manifest 与本文 154 个 key 精确一致且全部 enabled。某代码只出现在官方目录、prompt 或本文表格中，不算已经接通。运行时还要按地点与日期检查实际 availability；不可用时返回无数据/证据不足事实，不能换成名称相近参数。

数据通道缩写：

- `TS+S`：能力 adapter 同时支持 WDFN continuous/daily 与 Samples；实际使用哪条通道由站点、时间和 metadata 决定，不表示两边必然都有数据。
- `TS`：首选 WDFN time-series；仍须先查 metadata。
- `S`：USGS Samples 离散样品；不得把离散样品伪装成连续传感器序列。

`usgs:00060` discharge 是唯一的 `required_support`，每次分析自动加入；它同时允许作为用户目标。其他 153 项首期都是 `target`。单次 run 最多选择 6 个不同用户目标参数，系统加入的 discharge 不占该上限。

## 3. 能力覆盖摘要

| USGS 分组 | 数量 | 主要覆盖的用户问题 |
|---|---:|---|
| PHY | 28 | 流量、水位、流速、水温、降水、pH、电导率、浊度、盐度、固体和氧需求 |
| INN | 10 | 溶解氧、碱度、碳酸盐、氯化物、硫酸盐、氟化物、二氧化硅、溴化物 |
| NUT | 25 | 氨、硝酸盐、亚硝酸盐、总氮、磷酸盐、正磷酸盐、总磷和有机磷 |
| INM | 6 | 钙、镁、钠、钾和钠吸附比 |
| IMN | 7 | 砷、硼、锑、硒和碘化物 |
| IMM | 22 | 常见溶解金属与汞 |
| MBI | 12 | 总大肠菌群、粪大肠菌群、大肠杆菌、肠球菌、原生动物和噬菌体 |
| BIO | 9 | 叶绿素、蓝藻指标、浮游生物、底栖无脊椎和附着生物 |
| OPE | 18 | 常见除草剂、杀虫剂和农药降解/伴生指标 |
| OOT | 10 | 有机碳及常见挥发性有机污染物 |
| RAD | 5 | 铀、总 α/β 和镭-228 |
| SED | 2 | 悬浮沉积物浓度和输移量 |
| **合计** | **154** | 第一版正式联调能力基线 |

## 4. 154 项明细

名称和 official unit 保留 USGS 返回语义；实施时 canonical unit 使用项目统一拼写，但不得改变数量含义、样品 fraction 或测量方法。

### 4.1 PHY — Physical（28）

| ParameterKey | USGS 名称 | official unit | 通道 | 角色 |
|---|---|---|---|---|
| `usgs:00004` | Stream width | `ft` | TS+S | target |
| `usgs:00010` | Temperature, water | `deg C` | TS+S | target |
| `usgs:00045` | Precipitation | `in` | TS | target |
| `usgs:00055` | Stream velocity | `ft/sec` | TS+S | target |
| `usgs:00060` | Discharge | `ft3/s` | TS+S | target, required_support |
| `usgs:00061` | Discharge, instantaneous | `ft3/s` | TS | target |
| `usgs:00065` | Gage height | `ft` | TS | target |
| `usgs:00074` | Light transmission | `%` | TS+S | target |
| `usgs:00078` | Transparency, Secchi | `m` | S | target |
| `usgs:00080` | Color | `PCU` | S | target |
| `usgs:00081` | Apparent color | `PCU` | S | target |
| `usgs:00090` | Redox potential | `mV` | TS+S | target |
| `usgs:00095` | Specific conductance at 25C | `uS/cm @25C` | TS+S | target |
| `usgs:00310` | BOD, 5 day, 20 deg | `mg/l` | S | target |
| `usgs:00340` | COD, high level, unfiltered | `mg/l` | S | target |
| `usgs:00400` | pH | `standard units` | TS+S | target |
| `usgs:00403` | pH, unfiltered, laboratory | `standard units` | S | target |
| `usgs:00480` | Salinity | `ppth` | TS+S | target |
| `usgs:00500` | Total solids dried at 105C | `mg/l` | S | target |
| `usgs:00515` | Dissolved solids at 105C | `mg/l` | S | target |
| `usgs:00530` | Suspended solids | `mg/l` | S | target |
| `usgs:00535` | Loss on ignition of suspended solids | `mg/l` | S | target |
| `usgs:00900` | Hardness, water | `mg/l CaCO3` | S | target |
| `usgs:53048` | Transparency, Secchi tube | `cm` | S | target |
| `usgs:61028` | Turbidity, field | `NTU` | TS+S | target |
| `usgs:63680` | Turbidity, Formazin Nephelometric | `FNU` | TS+S | target |
| `usgs:70300` | Dissolved solids dried at 180C | `mg/l` | S | target |
| `usgs:90860` | Salinity at 25C | `PSU` | TS+S | target |

### 4.2 INN — Major anions / oxygen / alkalinity（10）

| ParameterKey | USGS 名称 | official unit | 通道 | 角色 |
|---|---|---|---|---|
| `usgs:00300` | Dissolved oxygen | `mg/l` | TS+S | target |
| `usgs:00301` | Dissolved oxygen, percent saturation | `% saturation` | TS+S | target |
| `usgs:00410` | Acid neutralizing capacity, field | `mg/l CaCO3` | S | target |
| `usgs:00440` | Bicarbonate, field | `mg/l` | S | target |
| `usgs:00445` | Carbonate, field | `mg/l` | S | target |
| `usgs:00940` | Chloride, filtered | `mg/l` | S | target |
| `usgs:00945` | Sulfate, filtered | `mg/l` | S | target |
| `usgs:00950` | Fluoride, filtered | `mg/l` | S | target |
| `usgs:00955` | Silica, filtered | `mg/l` | S | target |
| `usgs:71870` | Bromide, filtered | `mg/l` | S | target |

### 4.3 NUT — Nutrients（25）

| ParameterKey | USGS 名称 | official unit | 通道 | 角色 |
|---|---|---|---|---|
| `usgs:00600` | Total nitrogen, unfiltered | `mg/l` | TS+S | target |
| `usgs:00602` | Total nitrogen, filtered | `mg/l` | S | target |
| `usgs:00605` | Organic nitrogen, unfiltered | `mg/l as N` | S | target |
| `usgs:00607` | Organic nitrogen, filtered | `mg/l as N` | S | target |
| `usgs:00608` | Ammonia, filtered | `mg/l as N` | TS+S | target |
| `usgs:00610` | Ammonia, unfiltered | `mg/l as N` | S | target |
| `usgs:00613` | Nitrite, filtered | `mg/l as N` | TS+S | target |
| `usgs:00615` | Nitrite, unfiltered | `mg/l as N` | S | target |
| `usgs:00618` | Nitrate, filtered | `mg/l as N` | TS+S | target |
| `usgs:00620` | Nitrate, unfiltered | `mg/l as N` | S | target |
| `usgs:00623` | Ammonia plus organic nitrogen, filtered | `mg/l as N` | S | target |
| `usgs:00625` | Ammonia plus organic nitrogen, unfiltered | `mg/l as N` | S | target |
| `usgs:00630` | Nitrate plus nitrite, unfiltered | `mg/l as N` | S | target |
| `usgs:00631` | Nitrate plus nitrite, filtered | `mg/l as N` | TS+S | target |
| `usgs:00650` | Phosphate, unfiltered | `mg/l` | S | target |
| `usgs:00653` | Phosphate, filtered | `mg/l` | S | target |
| `usgs:00660` | Orthophosphate, filtered as PO4 | `mg/l as PO4` | TS+S | target |
| `usgs:00665` | Phosphorus, unfiltered | `mg/l as P` | TS+S | target |
| `usgs:00666` | Phosphorus, filtered | `mg/l as P` | S | target |
| `usgs:00667` | Phosphorus, suspended sediment | `mg/l as P` | S | target |
| `usgs:00669` | Hydrolyzable phosphorus, unfiltered | `mg/l as P` | S | target |
| `usgs:00670` | Organic phosphorus, unfiltered | `mg/l as P` | S | target |
| `usgs:00671` | Orthophosphate, filtered as P | `mg/l as P` | TS+S | target |
| `usgs:00672` | Hydrolyzable phosphorus, filtered | `mg/l as P` | S | target |
| `usgs:00673` | Organic phosphorus, filtered | `mg/l as P` | S | target |

### 4.4 INM — Major cations（6）

| ParameterKey | USGS 名称 | official unit | 通道 | 角色 |
|---|---|---|---|---|
| `usgs:00915` | Calcium, filtered | `mg/l` | S | target |
| `usgs:00916` | Calcium, unfiltered, recoverable | `mg/l` | S | target |
| `usgs:00925` | Magnesium, filtered | `mg/l` | S | target |
| `usgs:00930` | Sodium, filtered | `mg/l` | S | target |
| `usgs:00931` | Sodium adsorption ratio | `number` | S | target |
| `usgs:00935` | Potassium, filtered | `mg/l` | S | target |

### 4.5 IMN — Trace nonmetals（7）

| ParameterKey | USGS 名称 | official unit | 通道 | 角色 |
|---|---|---|---|---|
| `usgs:01000` | Arsenic, filtered | `ug/l` | S | target |
| `usgs:01002` | Arsenic, unfiltered | `ug/l` | S | target |
| `usgs:01020` | Boron, filtered | `ug/l` | S | target |
| `usgs:01095` | Antimony, filtered | `ug/l` | S | target |
| `usgs:01097` | Antimony, unfiltered | `ug/l` | S | target |
| `usgs:01145` | Selenium, filtered | `ug/l` | S | target |
| `usgs:71865` | Iodide, filtered | `mg/l` | S | target |

### 4.6 IMM — Trace metals（22）

| ParameterKey | USGS 名称 | official unit | 通道 | 角色 |
|---|---|---|---|---|
| `usgs:01005` | Barium, filtered | `ug/l` | S | target |
| `usgs:01010` | Beryllium, filtered | `ug/l` | S | target |
| `usgs:01025` | Cadmium, filtered | `ug/l` | S | target |
| `usgs:01030` | Chromium, filtered | `ug/l` | S | target |
| `usgs:01032` | Chromium(VI), filtered | `ug/l as Cr` | S | target |
| `usgs:01035` | Cobalt, filtered | `ug/l` | S | target |
| `usgs:01040` | Copper, filtered | `ug/l` | S | target |
| `usgs:01046` | Iron, filtered | `ug/l` | S | target |
| `usgs:01049` | Lead, filtered | `ug/l` | S | target |
| `usgs:01056` | Manganese, filtered | `ug/l` | S | target |
| `usgs:01057` | Thallium, filtered | `ug/l` | S | target |
| `usgs:01060` | Molybdenum, filtered | `ug/l` | S | target |
| `usgs:01065` | Nickel, filtered | `ug/l` | S | target |
| `usgs:01075` | Silver, filtered | `ug/l` | S | target |
| `usgs:01080` | Strontium, filtered | `ug/l` | S | target |
| `usgs:01085` | Vanadium, filtered | `ug/l` | S | target |
| `usgs:01090` | Zinc, filtered | `ug/l` | S | target |
| `usgs:01100` | Tin, filtered | `ug/l` | S | target |
| `usgs:01106` | Aluminum, filtered | `ug/l` | S | target |
| `usgs:01130` | Lithium, filtered | `ug/l` | S | target |
| `usgs:01150` | Titanium, filtered | `ug/l` | S | target |
| `usgs:50287` | Mercury, filtered | `ng/l` | S | target |

### 4.7 MBI — Microbiological（12）

| ParameterKey | USGS 名称 | official unit | 通道 | 角色 |
|---|---|---|---|---|
| `usgs:31501` | Total coliforms, M-Endo MF | `cfu/100ml` | S | target |
| `usgs:31625` | Fecal coliforms, M-FC MF | `cfu/100ml` | S | target |
| `usgs:31633` | Escherichia coli, m-TEC MF | `cfu/100ml` | S | target |
| `usgs:31649` | Enterococci, m-E MF | `cfu/100ml` | S | target |
| `usgs:50468` | E. coli, defined substrate test | `MPN/100 ml` | S | target |
| `usgs:50569` | Total coliforms, defined substrate test | `MPN/100 ml` | S | target |
| `usgs:61214` | Fecal streptococci | `cfu/100ml` | S | target |
| `usgs:61229` | Giardia | `cysts/100L` | S | target |
| `usgs:61230` | Cryptosporidium | `oocysts/100L` | S | target |
| `usgs:90900` | Total coliforms, MI MF | `cfu/100ml` | S | target |
| `usgs:90901` | E. coli, MI MF | `cfu/100ml` | S | target |
| `usgs:90903` | Coliphage, E. coli CN13 host | `pfu/100ml` | S | target |

### 4.8 BIO — Biological（9）

| ParameterKey | USGS 名称 | official unit | 通道 | 角色 |
|---|---|---|---|---|
| `usgs:32209` | Chlorophyll a, fluorometric, corrected | `ug/l` | TS+S | target |
| `usgs:32213` | Pheophytin a, fluorometric | `ug/l` | S | target |
| `usgs:32329` | Phycocyanin, sensor regression | `ug/l` | TS+S | target |
| `usgs:50223` | Phycocyanin, fluorometric | `ug/l` | TS+S | target |
| `usgs:50227` | Phytoplankton | `cells/L` | S | target |
| `usgs:60050` | Phytoplankton, total | `#/ml` | S | target |
| `usgs:70943` | Benthic invertebrates, total | `#/m2` | S | target |
| `usgs:70945` | Periphyton, total | `cells/mm2` | S | target |
| `usgs:70946` | Zooplankton, total | `#/m3` | S | target |

### 4.9 OPE — Pesticides（18）

| ParameterKey | USGS 名称 | official unit | 通道 | 角色 |
|---|---|---|---|---|
| `usgs:04035` | Simazine, filtered | `ug/l` | S | target |
| `usgs:04095` | Fonofos, filtered | `ug/l` | S | target |
| `usgs:34655` | p,p'-DDT, filtered | `ug/l` | S | target |
| `usgs:38442` | Dicamba, filtered | `ug/l` | S | target |
| `usgs:38933` | Chlorpyrifos, filtered | `ug/l` | S | target |
| `usgs:39341` | Lindane, filtered | `ug/l` | S | target |
| `usgs:39381` | Dieldrin, filtered | `ug/l` | S | target |
| `usgs:39415` | Metolachlor, filtered | `ug/l` | S | target |
| `usgs:39572` | Diazinon, filtered | `ug/l` | S | target |
| `usgs:39632` | Atrazine, filtered | `ug/l` | S | target |
| `usgs:39818` | Imidacloprid, unfiltered | `ug/l` | S | target |
| `usgs:39941` | Glyphosate, unfiltered | `ug/l` | S | target |
| `usgs:46342` | Alachlor, filtered | `ug/l` | S | target |
| `usgs:49260` | Acetochlor, filtered | `ug/l` | S | target |
| `usgs:49291` | Picloram, filtered | `ug/l` | S | target |
| `usgs:49309` | Carbofuran, filtered | `ug/l` | S | target |
| `usgs:49310` | Carbaryl, filtered | `ug/l` | S | target |
| `usgs:82660` | 2,6-Diethylaniline, filtered | `ug/l` | S | target |

### 4.10 OOT — Organic, other（10）

| ParameterKey | USGS 名称 | official unit | 通道 | 角色 |
|---|---|---|---|---|
| `usgs:00680` | Organic carbon, unfiltered | `mg/l` | S | target |
| `usgs:00681` | Organic carbon, filtered | `mg/l` | S | target |
| `usgs:34010` | Toluene, unfiltered | `ug/l` | S | target |
| `usgs:34030` | Benzene, unfiltered | `ug/l` | S | target |
| `usgs:34371` | Ethylbenzene, unfiltered | `ug/l` | S | target |
| `usgs:34423` | Dichloromethane, unfiltered | `ug/l` | S | target |
| `usgs:34475` | Tetrachloroethene, unfiltered | `ug/l` | S | target |
| `usgs:39175` | Vinyl chloride, unfiltered | `ug/l` | S | target |
| `usgs:39180` | Trichloroethene, unfiltered | `ug/l` | S | target |
| `usgs:78032` | Methyl tert-butyl ether, unfiltered | `ug/l` | S | target |

### 4.11 RAD — Radiochemical（5）

| ParameterKey | USGS 名称 | official unit | 通道 | 角色 |
|---|---|---|---|---|
| `usgs:22703` | Uranium, natural, filtered | `ug/l` | S | target |
| `usgs:80020` | Uranium, extraction fluorometric, filtered | `ug/l` | S | target |
| `usgs:80030` | Gross alpha, natural uranium curve, filtered | `ug/l` | S | target |
| `usgs:80050` | Gross beta, Sr-90/Y-90 curve, filtered | `pCi/L` | S | target |
| `usgs:81366` | Radium-228, filtered | `pCi/L` | S | target |

### 4.12 SED — Sediment（2）

| ParameterKey | USGS 名称 | official unit | 通道 | 角色 |
|---|---|---|---|---|
| `usgs:80154` | Suspended sediment concentration | `mg/l` | TS+S | target |
| `usgs:80155` | Suspended sediment discharge | `tons/day` | TS+S | target |

## 5. 解析、availability 与 evidence 约束

- Parser prompt 可以提供少量跨问题族 few-shot 和本目录的规范名称/aliases，但不得把 154 项展开成硬编码意图模板。LLM 仍要从“是否适合游泳”“藻华风险”“雨后是否浑浊”等未列模板目标自主推测所需参数；resolver 再确定性映射并给出 explicit/inferred 与简短 rationale。
- 名称相近但介质、filtered/unfiltered、方法或单位不同的代码不得自动合并。例如 `00400` field pH 与 `00403` laboratory pH、`63680` FNU 与 `61028` NTU、多个 E. coli 方法都是不同能力。用户意图不足以消歧时返回 `parameter_ambiguous`。
- 单次最多 6 个用户目标。宽泛目标自主推测后若需要超过 6 项，返回 `too_many_parameters` 并提示缩小问题；不得静默取前 6 项。discharge 自动加入且不占这 6 项。
- availability 按“ParameterKey × time segment × station/COMID”检查。表中 `TS+S` 不是自动 fallback 顺序；两条通道都必须保留来源、采样方法、时间精度和质量事实。
- 对未来日期，历史/近期观测只能作为预测 evidence，不能宣称未来已有实测值。对于只有离散样品的能力，若没有经 M5/M6 明确登记的侧面证据，完全无证据就跳过水体 LLM 推理。

## 6. 实施验收

后续 implementation TODO 应把本目录转成机器可校验 fixture，而不是复制另一份手写列表。至少验证：

- 154 个唯一 `ParameterKey`，官方精确代码查询全部命中，分组计数为 `28+10+25+6+7+22+12+9+18+10+5+2=154`；
- manifest 与本文 key 集合双向一致，且 `usgs:00060` 是唯一 required support；
- 每项 enabled capability 都具备 canonical unit、aliases、provider、availability、evidence rules 和 fixtures；
- 六项用户目标成功，第七项整体失败且不进入 topology；supporting discharge 不占上限、不重复；
- filtered/unfiltered、field/lab、单位或方法不同的近义代码触发正确消歧；
- WDFN metadata、Samples summary 和 observation 的 unavailable/empty/invalid 响应保持不同错误语义；
- 完全无 evidence 时不调用水体模型，允许的侧面 evidence 会降低置信度并保留 provenance。
