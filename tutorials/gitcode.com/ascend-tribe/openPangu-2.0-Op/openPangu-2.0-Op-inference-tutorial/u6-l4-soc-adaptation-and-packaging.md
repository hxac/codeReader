# u6-l4 SOC 适配与算子包发布

## 1. 本讲目标

本讲是第 6 单元的倒数第二讲，也是把前五单元所有知识「装进同一个发布流程」的收官之讲。学完后你应该能够：

1. 说出多 SOC（ascend910b / ascend910_93 / ascend950）适配在源码中的三个落点：OpDef 的 `AddConfig`、`op_host/config/<soc>/` 下的三类配置文件、以及 kernel/tiling 侧的 SOC 特化分支。
2. 跟踪 `--tiling_key` 参数从 `build.sh` 命令行一路传递到 opc 二进制编译的完整链路，理解它如何「裁剪」TilingKey 分支以加速编译。
3. 画出一次完整发布的流程图：build.sh 参数 → CMake 三大目标（opapi/opsproto/optiling）+ 二进制编译 → CPack run 包 → 安装到 `opp/vendors` → `source set_env.bash` → torch_ops_extension wheel 包。
4. 拿到一块新芯片时，知道需要新增/修改哪些文件才能让整套算子库跑起来。

本讲承接 u1-l2（build.sh 参数翻译与三个产物库）和 u5-l1（TilingKey 编码与模板轮询）：u1-l2 讲了「build.sh 是参数翻译器」，本讲补上当时按下未表的 `--tiling_key` 与 `--disable-check-compatible` 的最终落点；u5-l1 讲了「host 侧算 TilingKey、kernel 侧对号入座」，本讲讲「编译期如何按 TilingKey 裁剪二进制」。

## 2. 前置知识

- **SOC（System on Chip）**：一颗昇腾芯片的型号。本仓库涉及三类：`ascend910b`（910B 系列，A2 环境镜像）、`ascend910_93`（910 系列的一个派生版本，A3 环境镜像，也是仓库的**默认编译目标**）、`ascend950`（950 系列，A5 环境镜像，仓库内部常称 arch35/regbase）。README 中明确列出了这三个合法取值。
- **同名不同芯**：同一段 AscendC kernel 源码，在不同 SOC 上编译产物完全不同（指令集、核数、UB/L1 大小都有差异），所以「适配多 SOC」本质上是「一份源码 × N 份编译期配置 × N 份二进制产物」。
- **binary.json（二进制清单）**：CANN 自定义算子体系里，`op_host/config/<soc>/` 下的 `*_binary.json` 声明该算子在这块芯片上要**预编译**哪些「dtype/格式组合」的二进制，每条组合对应一个带内容哈希的 `bin_filename`。
- **simplified_key.ini（简化键模式）**：控制 opc 编译工具的 `--simplified_key_mode` 取值，决定编译期如何把「输入组合」压缩成简化的检索键。
- **runtime_kb.json（运行期知识库）**：matmul 类算子专属，按「形状指纹 → 调优参数」记录离线调优结果，文件名形如 `Ascend910B1_24_AiCore_AiInfraMatmul_runtime_kb.json`（含具体型号与 AICore 数量）。
- **vendors 目录与 set_env.bash**：CANN 通过 `opp/vendors/<厂商名>/` 目录承载第三方自定义算子；run 包安装后会在该目录下生成 `bin/set_env.bash`，source 它即可把自定义算子路径注入环境变量。
- **run 包与 wheel 包双包协作**：u1-l1/u3-l4 已建立的概念——run 包是「发动机」（aclnn/原型/tiling/二进制），wheel 包（omni_custom_ops）是「方向盘」（torch 侧注册），必须先装 run 包后装 wheel 包。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ascendc/build.sh` | 编译入口：解析 `-c/-n/--tiling_key/--disable-check-compatible` 等参数并翻译成 CMake 变量 |
| `ascendc/CMakeLists.txt` | 顶层工程：定义 opapi/opsproto/optiling 三大目标、按 SOC 循环触发二进制编译、CPack 打 run 包 |
| `ascendc/cmake/config.cmake` | 编译配置：CHECK_COMPATIBLE 版本校验、prepare.sh 调用、各安装目录变量 |
| `ascendc/cmake/func.cmake` | 构建函数库：`add_ops_tiling_keys`（tiling key 落盘）、`add_bin_compile_target`（按 SOC 编译并安装二进制） |
| `ascendc/cmake/scripts/prepare.sh` | 配置阶段脚本：起内层 CMake 预生成 autogen 与编译脚本 |
| `ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/config/ascend910b/moe_init_routing_v3_binary.json` | MoE 算子在 910b 上的预编译二进制清单（7 条 dtype 组合） |
| `ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/config/ascend910b/moe_init_routing_v3_simplified_key.ini` | MoE 算子的 opc `--simplified_key_mode` 配置 |
| `ascendc/src/ops-nn/matmul/ai_infra_matmul/op_host/config/ascend910b/Ascend910B1_24_AiCore_AiInfraMatmul_runtime_kb.json` | matmul 在 910b 上的运行期调优知识库（117 条记录） |
| `ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_def.cpp` | MoE 算子 OpDef：`AddConfig` 声明支持的 SOC |
| `ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_def.cpp` | 950 适配范本：独立 `config_950` 原型 + `USE_ASCEND950` 宏开关 |
| `ascendc/torch_ops_extension/build_and_install.sh` / `setup.py` | wheel 包编译安装脚本 |
| `ascendc/README.md` | 官方编译/安装命令与 FAQ |

## 4. 核心概念与源码讲解

### 4.1 SOC 配置：AddConfig 准入声明与 op_host/config 三类文件

#### 4.1.1 概念说明

「算子支持某块芯片」在 CANN 体系里不是一句空话，而是三层声明的叠加：

1. **准入层（OpDef 的 `AddConfig`）**：算子原型注册时，`this->AICore().AddConfig("<soc>")` 逐个登记该算子允许运行在哪些 SOC 上。没登记的芯片，图引擎/aclnn 执行器在查表时直接找不到这个算子。
2. **配置层（`op_host/config/<soc>/`）**：按 SOC 子目录存放差异化编译配置——`*_binary.json`（预编译二进制清单）、`*_simplified_key.ini`（简化键模式）、`*_runtime_kb.json`（运行期调优知识库）。注意这是**按需存在**的：MoE 算子只有 `ascend910b/` 和 `ascend910_93/` 两个子目录（没有 950），matmul 只有 `ascend910b/` 一个——没有差异化需求的 SOC 就不需要目录。
3. **实现层（kernel/tiling 的 SOC 特化代码）**：如 u4-l7 讲过的 `arch35/` 专属 kernel、`isRegbase_` 交棒逻辑等。

本模块聚焦前两层；第三层在前面的算子族讲义中已充分展开。

#### 4.1.2 核心流程

一块芯片上跑起一个算子的「准入 + 配置」流程：

```text
OpDef 注册（AddConfig("ascend910b") 等）
        │
        ▼
编译期：build.sh -c <soc> ──► CMake 按 ASCEND_COMPUTE_UNIT 循环
        │                       │
        │                       ├─ 读取 op_host/config/<soc>/*.json|ini
        │                       │   （binary.json 决定预编译哪些 dtype 组合）
        │                       ▼
        │                  opc 编译出 kernel 二进制（bin_filename 带内容哈希）
        │                       │
        ▼                       ▼
运行期：图引擎按 OpDef 查表确认 SOC 支持 ──► 按 binary_info_config.json
        定位预编译二进制 ──► host 侧 tiling 算出 TilingKey ──► 命中二进制执行
```

#### 4.1.3 源码精读

**（1）最简形式：MoE 算子的双 SOC 声明**

[ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_def.cpp:L114-L119](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/ai_infra_moe_init_routing_v3_def.cpp#L114-L119) 中，MoE 算子在 OpDef 构造函数末尾用两行 `AddConfig` 声明它只支持 910b 和 910_93：

```cpp
this->AICore().AddConfig("ascend910b");
this->AICore().AddConfig("ascend910_93");
```

这与该算子 `op_host/config/` 下恰好只有 `ascend910b/`、`ascend910_93/` 两个子目录严格对应——**AddConfig 声明的 SOC 集合与 config 子目录、以及 u4-l5 讲过的 TilingKey 编码是三位一体的**。全仓库检索 `*_def.cpp` 中 `AddConfig` 的 SOC 参数，共出现五种取值：`ascend910b` 与 `ascend910_93` 各 16 处（主流写法，成对声明，写法上有的带第二参数传独立配置对象，如 `AddConfig("ascend910b", aicore_config)`）、`ascend950` 3 处（全部在宏守卫内，见下）、`kirinx90/kirin9030` 各 1 处（另一类芯片形态）。

**（2）进阶形式：950 的独立原型 + 编译期宏守卫**

910b 与 910_93 的输入输出规格一致，可以共用一份原型；而 950（regbase 架构）连 IO 签名都不同，需要一份独立的 `OpAICoreConfig`。[ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_def.cpp:L254-L257](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_def.cpp#L254-L257) 先声明两块常规芯片，随后另起一份 `config_950`：

```cpp
this->AICore().AddConfig("ascend910b");
this->AICore().AddConfig("ascend910_93");

OpAICoreConfig config_950;
config_950.Input("kv")
    .DataType(kvDataTypeRegbase)
    .Format(formatRegbase)
    .UnknownShapeFormat(formatRegbase);
```

这份 `config_950` 从 L257 一直写到 L350（输入含 `kv/gamma/cos/sin/index/k_cache/ckv_cache/k_rope_scale/c_kv_scale/k_rope_offset/c_kv_offset/v`，输出为 `k_cache/ckv_cache/k_rope/c_kv`），最后由宏守卫决定是否登记，见 [同文件:L351-L356](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_def.cpp#L351-L356)：

```cpp
#ifdef USE_ASCEND950
this->AICore().AddConfig("ascend950", config_950);
#endif
OpAICoreConfig config_kirin = GetKirinCoreConfig();
this->AICore().AddConfig("kirinx90", config_kirin);
this->AICore().AddConfig("kirin9030", config_kirin);
```

注意两点：`AddConfig` 的第二个重载接收**独立的原型配置对象**（与前面无参重载共用默认配置形成对比）；`kirinx90/kirin9030` 是另外两类芯片形态（通过 `GetKirinCoreConfig()` 获取原型）。

宏 `USE_ASCEND950` 由算子级 CMakeLists 在目标芯片是 950 时注入，见 [ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/CMakeLists.txt:L17-L20](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/CMakeLists.txt#L17-L20)：

```cmake
if (ASCEND_COMPUTE_UNIT STREQUAL "ascend950")
    target_compile_definitions(op_host_aclnnExc PRIVATE -DUSE_ASCEND950=1)
    target_compile_definitions(optiling PRIVATE -DUSE_ASCEND950=1)
endif()
```

即 `-c ascend950` 的编译目标里才会编出 950 原型；`ai_infra_sparse_flash_attention_pioneer` 等算子用了同样的手法（守卫的是 `op_host_aclnnInner`）。这就是「**一份源码，编译期裁剪出每块芯片的原型**」。

**（3）binary.json：预编译二进制清单**

[ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/config/ascend910b/moe_init_routing_v3_binary.json:L2-L16](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/config/ascend910b/moe_init_routing_v3_binary.json#L2-L16) 是该文件的第一条清单项：

```json
{
  "op_type": "MoeInitRoutingV3",
  "op_list": [
    {
      "bin_filename": "MoeInitRoutingV3_19802a760bc5088fa9a91fb0460dbe6b2ab7780",
      "inputs": [
        { "name": "x", "index": 0, "dtype": "int8", "format": "ND",
          "paramType": "required", "shape": [-2] },
        ...
```

要点解读：

- `op_type` 用的是 OpDef 类名 `MoeInitRoutingV3`（再次印证 u2-l1 的「类名是全局关联键」）。
- 每条清单项 = 一种 dtype 组合 + 一个 `bin_filename`。文件名后缀 `19802a760…` 是**内容哈希**——这呼应 func.cmake 编译命令里的环境变量 `BIN_FILENAME_HASHED=1`（见 4.3.3），二进制文件名带哈希可以在多版本共存时精确寻址。
- 全文件共 **7 条**组合（`bin_filename` 分别在 L5、L142、L279、L416、L553、L690、L827）：`x` 为 int8/fp16/fp32/bf16 四种「同 dtype 出」组合，外加 fp32→int8、bf16→int8、fp16→int8 三种「反量化出」组合（对应 u4-l5 讲过的 quantMode 语义）；`expert_idx` 恒为 int32，`expanded_row_idx` 恒为 int32，`expert_tokens_count_or_cumsum` 恒为 int64。
- `shape: [-2]` 表示任意秩的动态形状（与 OpDef 的动态 shape 开关对齐）。
- attrs 段（如 [L90-L139](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/config/ascend910b/moe_init_routing_v3_binary.json#L90-L139)）把 OpDef 的属性默认值复述一遍，供编译工具核对。
- `ascend910_93/` 子目录下有同名的一对文件，内容结构相同（组合数一致），体现「同一算子在不同芯片上的两份清单」。

**（4）simplified_key.ini：opc 的简化键模式**

[ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/config/ascend910b/moe_init_routing_v3_simplified_key.ini:L9-L21](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/op_host/config/ascend910b/moe_init_routing_v3_simplified_key.ini#L9-L21)：

```ini
; 该文件主要影响 opc 工具 编译二进制kernel时， --simplified_key_mode 选项中填写的值，格式如下所示：
; [某算子]
; default=xx
; ascendxx=xx
; ...
[MoeInitRoutingV3]
default=0
```

文件自带的注释（L9-L19）说得很清楚：`default` 是默认 mode，`ascendxx=xx` 是特定芯片的差异化取值；不配置时 AscendC 算子按 `simplified_key_mode=0` 处理。「简化键」影响的是编译期把输入组合折叠成检索键的方式——这与 u4-l8 讲过的 host 侧 `GenSimplifiedKey`（matmul 运行期查 runtime_kb.json 用的键）是同一思想在编译期与运行期的两次落地。

**（5）runtime_kb.json：matmul 的运行期调优知识库**

`ascendc/src/ops-nn/matmul/ai_infra_matmul/op_host/config/ascend910b/` 下有 6 个文件：`ai_infra_matmul_binary.json` 加 5 个 runtime 知识库，文件名精确到**具体型号与 AICore 数量**：

```text
Ascend910B1_24_AiCore_AiInfraMatmul_runtime_kb.json    （910B1，24 核）
Ascend910B2_24_AiCore_AiInfraMatmul_runtime_kb.json
Ascend910B2C_24_AiCore_AiInfraMatmul_runtime_kb.json
Ascend910B3_20_AiCore_AiInfraMatmul_runtime_kb.json    （910B3，20 核）
Ascend910B4_20_AiCore_AiInfraMatmul_runtime_kb.json
```

每个文件是 JSONL 格式（一行一条记录，共 117 行）。[Ascend910B1_24_AiCore_AiInfraMatmul_runtime_kb.json:L1](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/matmul/ai_infra_matmul/op_host/config/ascend910b/Ascend910B1_24_AiCore_AiInfraMatmul_runtime_kb.json#L1) 的第一条记录：

```json
{"id":1836242645,
 "info_dict":{"a_dtype":1,"a_format":2,...,"k":5120,"m":8192,"n":6272,
              "trans_a_flag":false,"trans_b_flag":false,...},
 "knowledge":{"usedCoreNum":24,"singleCoreM":256,"singleCoreN":128,"singleCoreK":5120,
              "baseM":256,"baseN":128,"baseK":64,"depthA1":8,"depthB1":8,
              "stepKa":4,"stepKb":4,"dbL0A":2,"dbL0B":2,"dbL0C":1,
              "l2MTileCnt":2,"l2NTileCnt":3,"l2MTileBlock":16,"l2NTileBlock":17,...},
 "op":"AiInfraMatmul","version":0}
```

三段结构：`info_dict` 是**形状/类型指纹**（m/n/k、转置标志、dtype 枚举值），`knowledge` 是**调优结论**（用多少核、单核切多大的 M/N/K tile、L0A/L0B/L0C 双缓冲深度、L2 分块——这些正是 u4-l8 讲过的 matmul tiling 参数），`id` 是这条记录的检索哈希。运行期 host 侧 tiling 用 `GenSimplifiedKey` 拼出当前输入的指纹去查库，命中就直接采用离线调优结论，跳过在线求解。这也解释了为什么文件名要精确到 `B1_24/B3_20`：**24 核与 20 核的最优切分不同，知识库不能混用**。

#### 4.1.4 代码实践

**实践目标**：盘点「算子 × SOC × 配置文件」的对应关系，验证 AddConfig 与 config 子目录的一致性。

**操作步骤**（纯源码阅读，无需硬件）：

1. 在 `ascendc/` 下执行 `grep -rn 'AddConfig("' src --include='*_def.cpp' | grep -v kirin`，统计每个算子声明的 SOC 集合。
2. 执行 `find src -path '*op_host/config/*' -name '*.json' -o -path '*op_host/config/*' -name '*.ini' | sort`，列出全仓库所有 SOC 配置文件。
3. 交叉比对：哪些算子有 config 子目录？子目录名与 AddConfig 集合是否一致？
4. 打开 `ai_infra_kv_rms_norm_rope_cache_def.cpp`，对比默认配置与 `config_950` 的 IO 差异（提示：950 原型的输入里有 `k_rope_scale/c_kv_scale` 等 950 专用量化输入）。

**需要观察的现象**：仓库中拥有 `op_host/config/` 的算子很少（MoE、matmul 等个别算子），大多数算子只有 AddConfig 声明而没有 config 目录——因为 binary.json/simplified_key.ini/runtime_kb.json 只在「需要预编译裁剪或离线调优」时才必要。

**预期结果**：MoE 算子 `AddConfig("ascend910b") + AddConfig("ascend910_93")`，config 下恰好是 `ascend910b/`、`ascend910_93/` 两个子目录、各含一对 binary.json + simplified_key.ini；matmul 只有 `ascend910b/`（runtime 知识库仅覆盖 910b 系列，910_93 上由 legacy 公共库的调优缓存兜底，见 u5-l1）。若实测与预期不符，以实测为准并记录差异。

### 4.2 tiling key 编译：--tiling_key 参数的传递链与裁剪原理

#### 4.2.1 概念说明

u5-l1 讲过：一个算子往往有几十个 TilingKey 分支（MoE 有 30 余个 kernel 变体），每个分支都会被 opc 编译成一份二进制。全量编译很慢，于是 build.sh 提供 `--tiling_key` 参数，**只编译指定的 key 分支**。它不是运行期开关，而是纯编译期的「裁剪指令」。

配套的 `--disable-check-compatible` 则控制「本仓库与所装 CANN 包的版本配套校验」是否执行。

#### 4.2.2 核心流程

`--tiling_key` 的六级传递链：

```text
① build.sh 命令行：bash build.sh --tiling_key "1;2;3"
        │  （build.sh:L324-L327 解析入 TILING_KEY 变量）
        ▼
② CUSTOM_OPTION 追加 -DTILING_KEY=1;2;3
        │  （build.sh:L374-L376）
        ▼
③ 外层 CMake 配置期：config.cmake 把分号转成 "::" （EP_TILING_KEY），
   作为 --tiling-key 参数传给 prepare.sh
        │  （config.cmake:L197-L201、L223）
        ▼
④ prepare.sh 把 "::" 还原回 ";"，作为 -DTILING_KEY 传给内层 CMake，
   并 make prepare_build
        │  （prepare.sh:L51-L54、L92-L96、L120、L127）
        ▼
⑤ 顶层 CMakeLists 调 add_ops_tiling_keys(OP_NAME "ALL" TILING_KEYS ${TILING_KEY})
        │  （CMakeLists.txt:L276-L279）
        ▼
⑥ func.cmake 把它落盘为 custom_tiling_keys.ini 一行：
   "ALL,<soc>,1;2;3"，交 prepare_build 生成的编译脚本消费；
   或（ADD_OPS_COMPILE_OPTION_V2 路径）转成 --tiling_key=1,2,3 编译选项
        │  （func.cmake:L212-L228）
        ▼
   opc 按 key 列表裁剪二进制编译（配合 TILINGKEY_PAR_COMPILE=1 并行）
```

分号与 `::` 的来回替换是因为这些值要穿越「shell → CMake → shell」三层，分号在 CMake 里是列表分隔符、在 shell 里需要转义，`::` 是安全的中间载体。

#### 4.2.3 源码精读

**（1）入口解析与翻译**

[ascendc/build.sh:L324-L327](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L324-L327) 接收参数（`--tiling-key` 与 `--tiling_key` 两种拼法都接受）：

```bash
--tiling-key|--tiling_key)
    TILING_KEY="$2"
    shift 2
    ;;
```

[ascendc/build.sh:L374-L376](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L374-L376) 把它拼进 CMake 选项：

```bash
if [ -n "${TILING_KEY}" ];then
    CUSTOM_OPTION="${CUSTOM_OPTION} -DTILING_KEY=${TILING_KEY}"
fi
```

帮助文本见 [ascendc/build.sh:L58-L59](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L58-L59)：多个值用分号分隔、引号包裹，默认 all。

**（2）外层 CMake：转义后交给 prepare.sh**

[ascendc/cmake/config.cmake:L196-L201](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/config.cmake#L196-L201)：

```cmake
if (NOT PREPARE_BUILD AND ENABLE_OPS_KERNEL)
    if (TILING_KEY)
        string(REPLACE ";" "::" EP_TILING_KEY "${TILING_KEY}")
    else()
        set(EP_TILING_KEY FALSE)
    endif ()
```

随后 [config.cmake:L213-L232](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/config.cmake#L213-L232) 以 `--tiling-key ${EP_TILING_KEY}`、`--check-compatible ${CHECK_COMPATIBLE}`、`--ascend-computeunit ...` 等一串参数启动 `cmake/scripts/prepare.sh`——这就是 u1-l2 说的「配置阶段内层 CMake 预生成」，本讲补全了它携带的完整参数面。

**（3）prepare.sh：还原与二次注入**

[ascendc/cmake/scripts/prepare.sh:L92-L96](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/scripts/prepare.sh#L92-L96) 的 `convert_string` 把 `::` 还原成 `;`；[prepare.sh:L108-L128](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/scripts/prepare.sh#L108-L128) 用还原后的值配置**内层 CMake**（`-DPREPARE_BUILD=ON`）并 `make prepare_build`——预生成 autogen 代码与每个算子的二进制编译脚本。

**（4）落盘：custom_tiling_keys.ini**

内层构建完成后，顶层 CMake 走到 [ascendc/CMakeLists.txt:L276-L279](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L276-L279)：

```cmake
add_ops_tiling_keys(
        OP_NAME "ALL"
        TILING_KEYS ${TILING_KEY}
)
```

函数本体在 [ascendc/cmake/func.cmake:L212-L228](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/func.cmake#L212-L228)：未传 `TILING_KEYS` 时直接返回（默认全量）；传了则写入 `${ASCEND_CUSTOM_TILING_KEYS}`——该变量在 [config.cmake:L58](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/config.cmake#L58) 定义为 `${ASCEND_AUTOGEN_DIR}/custom_tiling_keys.ini`：

```cmake
file(APPEND ${ASCEND_CUSTOM_TILING_KEYS}
        "${OP_COMPILE_OP_NAME},${OP_COMPILE_COMPUTE_UNIT},${OP_COMPILE_TILING_KEYS}\n"
)
```

即 ini 里的一行 `ALL,ascend910_93,1;2;3`（CMake 列表展开时分号原样保留）。该 ini 位于 autogen 目录，由 prepare_build 阶段生成的编译脚本读取后约束 opc 只编译这些 key（其消费逻辑在 CANN 安装目录自带的构建脚本中，不在本仓库源码内，具体文件待确认）。另一条 `ADD_OPS_COMPILE_OPTION_V2` 路径则把 key 用逗号拼成 `--tiling_key=1,2,3` 编译选项（同函数 L216-L221）。

**（5）裁剪生效的现场：二进制编译命令**

[ascendc/cmake/func.cmake:L470-L479](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/func.cmake#L470-L479) 是每个算子每个 SOC 的二进制编译命令：

```cmake
list(APPEND _BUILD_COMMAND export TILINGKEY_PAR_COMPILE=1 &&)
list(APPEND _BUILD_COMMAND export BIN_FILENAME_HASHED=1 &&)
list(APPEND _BUILD_COMMAND bash ${bin_script} ${OP_SRC_OUT_DIR}/${op_type}.py ${OP_BIN_OUT_DIR})
```

`TILINGKEY_PAR_COMPILE=1` 说明按 TilingKey 切分时可并行编译；`BIN_FILENAME_HASHED=1` 呼应 4.1.3 中 binary.json 的哈希文件名；`${bin_script}` 是 prepare 阶段生成到 `gen/` 目录的 per-op 编译脚本，被裁剪信息正是通过它生效。

**（6）--disable-check-compatible 的真实语义**

先看代码事实：[ascendc/build.sh:L22](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L22) 预定义 `CHECK_COMPATIBLE="false"`；[build.sh:L304-L307](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L304-L307) 的开关也只是把它置为 `false`（兼容 `--disable-check-compatiable` 拼写）：

```bash
--disable-check-compatible|--disable-check-compatiable)
    CHECK_COMPATIBLE="false"
    shift
    ;;
```

最终在 [build.sh:L433](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L433) 拼入 `-DCHECK_COMPATIBLE=${CHECK_COMPATIBLE}`。消费端在 [ascendc/cmake/config.cmake:L175-L194](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/config.cmake#L175-L194)：开关打开时执行 `check_version_compatible.py`，用仓库根的 `version.info` 与所装 CANN 包比对，失败则 `FATAL_ERROR` 终止配置。

也就是说：**当前仓库里版本校验默认就是关闭的**，该参数更多是显式语义与兼容外部注入（如 CI 以环境变量方式置真）的保险。README 的 FAQ（[ascendc/README.md:L305](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/README.md#L305)）「使用不配套的 cann 包需要在算子编译时加上 `--disable-check-compatible`」是针对校验被打开的场景给出的解法。

#### 4.2.4 代码实践

**实践目标**：验证 `--tiling_key` 的传递链与裁剪效果。

**操作步骤**（有昇腾环境时；无环境时做 1、2、5 的静态追踪）：

1. 静态追踪：从 `build.sh --tiling_key "1"` 出发，手动模拟 ⑥ 级传递，写出每一站变量的值（`TILING_KEY=1` → `-DTILING_KEY=1` → `EP_TILING_KEY=1` → 内层 `-DTILING_KEY=1` → ini 行 `ALL,ascend910_93,1`）。
2. 阅读 [func.cmake:L212-L228](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/func.cmake#L212-L228)，回答：为什么未传 `TILING_KEYS` 时函数直接 return？此时 ini 文件是否还存在？（提示：看 [config.cmake:L239-L240](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/config.cmake#L239-L240) 的 `file(REMOVE)+file(TOUCH)`。）
3. （有环境）`bash build.sh -n 'ai_infra_moe_init_routing_v3' -c ascend910_93 --tiling_key "1"` 编译。
4. （有环境）去掉 `--tiling_key` 重编一次，对比两次的总耗时与 `build/` 下产出的 `.o`/二进制数量。
5. 检查生成的 `custom_tiling_keys.ini` 内容是否与你传入的 key 一致（在 autogen 输出目录下查找）。

**需要观察的现象**：带 `--tiling_key` 的编译明显更快；MoE 算子的二进制产物条目数变少；ini 文件中出现 `ALL,ascend910_93,1` 一行。

**预期结果**：裁剪只影响「编译出多少份二进制」，不影响 host 侧 tiling 代码与 OpDef——运行期若真命中被裁掉的 key，会找不到对应二进制（这是使用该参数必须自担的风险）。步骤 3-5 的具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `TILING_KEY` 在传递过程中要先把分号换成 `::`，到 prepare.sh 再换回来？

**参考答案**：这条值链要穿越 shell → CMake → shell 三层。分号在 CMake 中是列表分隔符（会被解释成列表而非字面量），在 shell 中又是命令分隔符；`::` 在两层里都是安全字面量，因此作为「运输形态」：config.cmake 用 `string(REPLACE ";" "::")` 打包，prepare.sh 的 `convert_string` 用 `sed 's/::/;/g'` 还原后再交给内层 CMake。

**练习 2**：`--tiling_key "1"` 编译出的 run 包拿到真机上运行， host 侧 tiling 算出了 TilingKey=2 的场景，会发生什么？

**参考答案**：host 侧 tiling 与 OpDef 都完整存在（它们编在 `cust_opmaster_rt2.0.so` 里，不受裁剪影响），SetTilingKey(2) 会正常落账；但 key=2 对应的 kernel 二进制在编译期被裁掉、没有打进包里，运行期按 key 检索二进制会失败，算子无法执行。所以 `--tiling_key` 只适合「明确知道目标场景只用这几个 key」的定向交付，全量发布必须不带该参数。

**练习 3**：`add_ops_tiling_keys(OP_NAME "ALL" ...)` 中 OP_NAME 为什么是 `ALL` 而不是具体算子名？

**参考答案**：顶层 CMakeLists 在 [L276-L279](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L276-L279) 把 build.sh 的全局 `--tiling_key` 落盘为一条作用于全部算子的记录（`ALL,<soc>,<keys>`）；函数本身支持按算子名精细配置（cmake_parse_arguments 的 OP_NAME 参数），只是当前仓库只用了全局粒度。

### 4.3 安装布局：三大目标 → vendors 目录 → run 包 → set_env.bash → wheel

#### 4.3.1 概念说明

u1-l2 讲过「三个动态库 + vendors 安装」，本讲把安装布局讲**精确**：每个目标安装到 vendors 树的哪个叶子节点、二进制与 config 装到哪里、run 包如何打出、装完之后 `set_env.bash` 做了什么、wheel 包又是如何接上的。这张「地图」是排查「符号找不到」「二进制不命中」类问题的底层依据。

#### 4.3.2 核心流程

一次完整发布的流水线：

```text
bash build.sh -c <soc> [-n '算子列表'] [--tiling_key '...']
   │
   ├─ CMake 目标 opapi     → libcust_opapi.so        → vendors/<V>/op_api/lib
   ├─ CMake 目标 opsproto  → libcust_opsproto_rt2.0.so → vendors/<V>/op_proto/lib/linux/<arch>/
   ├─ CMake 目标 optiling  → libcust_opmaster_rt2.0.so → vendors/<V>/op_impl/ai_core/tbe/op_tiling/lib/linux/<arch>/
   │                        └ liboptiling.so 兼容软链 → .../op_tiling/
   ├─ 每 SOC 循环：ops_kernel → kernel 二进制        → vendors/<V>/op_impl/ai_core/tbe/kernel/<soc>/
   │                              binary_info_config.json → .../kernel/config/<soc>/
   ├─ kernel 源码/实现脚本  → vendors/<V>/op_impl/ai_core/tbe/<V>_impl/ascendc/...
   ├─ version.info          → vendors/<V>/
   ├─ install.sh/upgrade.sh（sed 替换 vendor_name）
   ▼
CPack(External + makeself) → output/CANN-omni_custom_ops-<ver>-linux.<arch>.run
   │
   ▼  ./xxx.run --install-path=<toolkit>/opp
安装释放到 opp/vendors/omni_custom_transformer/，并生成 bin/set_env.bash
   │
   ▼  source .../vendors/omni_custom_transformer/bin/set_env.bash
环境变量注入（ASCEND_OPP_PATH / LD_LIBRARY_PATH 等指向自定义算子）
   │
   ▼  cd torch_ops_extension && bash build_and_install.sh
setup.py → dist/omni_custom_ops-1.0-*.whl → pip install
   │
   ▼  import omni_custom_ops
torch.ops.custom 命名空间可用（经 dlopen 找到上面 vendors 里的 libcust_opapi.so）
```

其中 `<V>` = `VENDOR_NAME` = `omni_custom_transformer`。

#### 4.3.3 源码精读

**（1）SOC 目标与厂商名的定义处**

[ascendc/CMakeLists.txt:L16-L18](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L16-L18)：

```cmake
set(ASCEND_COMPUTE_UNIT "ascend910_93"  CACHE STRING "soc that need to be compiled")
set(ASCEND_OP_NAME      "ALL"           CACHE STRING "operators that need to be compiled")
set(VENDOR_NAME         "omni_custom_transformer"     CACHE STRING "vendor name")
```

默认 SOC 是 `ascend910_93`；`VENDOR_NAME` 决定了 vendors 树下的厂商目录名（README 安装命令中的 `vendors/omni_custom_transformer` 即来源于此）。

**（2）三大动态库目标的安装路径**

- **opapi**：[CMakeLists.txt:L108-L155](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L108-L155)。目标名 `opapi`，输出名改写为 `cust_opapi`（L150-L152），安装到 `packages/vendors/${VENDOR_NAME}/op_api/lib`（L153-L155）。aclnn 头文件安装到 `op_api/include`（该变量定义见 [config.cmake:L77](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/config.cmake#L77)）。
- **opsproto**：[CMakeLists.txt:L158-L201](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L158-L201)。输出名 `cust_opsproto_rt2.0`（L196-L198），安装到 `op_proto/lib/linux/${CMAKE_SYSTEM_PROCESSOR}`（L199-L201）；自动生成的 `*_proto.h` 装到 `op_proto/inc`（L484-L486）。
- **optiling**：[CMakeLists.txt:L204-L274](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L204-L274)。输出名 `cust_opmaster_rt2.0`（L247-L249），安装到 `op_impl/ai_core/tbe/op_tiling/lib/linux/${CMAKE_SYSTEM_PROCESSOR}`（L255-L257）；L250-L254 还在构建目录做了指向 `TILING_CUSTOM_FILE`（`liboptiling.so`，见 [config.cmake:L88-L89](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/config.cmake#L88-L89)）的软链，L259-L274 再以 `optiling_compat` 目标把兼容软链装到 `op_tiling/` 下一级——给按旧路径 `liboptiling.so` 找 tiling 库的运行时兜底。

**（3）按 SOC 的二进制编译与安装**

[CMakeLists.txt:L707-L734](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L707-L734) 是 SOC 循环的两个入口：

```cmake
foreach (compute_unit ${ASCEND_COMPUTE_UNIT})
    add_compile_cmd_target(COMPUTE_UNIT ${compute_unit})
    add_ops_info_target(COMPUTE_UNIT ${compute_unit})
endforeach ()

if (ENABLE_OPS_KERNEL)
    ...
    foreach (compute_unit ${ASCEND_COMPUTE_UNIT})
        add_bin_compile_target(COMPUTE_UNIT ${compute_unit} OP_INFO ${OP_DIR_LIST})
    endforeach ()
endif ()
```

`ASCEND_COMPUTE_UNIT` 是列表——所以 `-c "ascend910b;ascend910_93"` 一次编两块芯片，每个目标各编一份。`add_bin_compile_target` 的安装逻辑在 [func.cmake:L328-L332](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/func.cmake#L328-L332)（`_INSTALL_DIR = packages/vendors/${VENDOR_NAME}/op_impl/ai_core/tbe/kernel`）与 [func.cmake:L422-L428](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/func.cmake#L422-L428)：

```cmake
install(DIRECTORY ${OP_BIN_OUT_DIR}
        DESTINATION ${_INSTALL_DIR}/${BINARY_COMPUTE_UNIT} OPTIONAL)
install(FILES ${BIN_OUT_DIR}/${op_file}.json
        DESTINATION ${_INSTALL_DIR}/config/${BINARY_COMPUTE_UNIT} OPTIONAL)
```

即 kernel 二进制进 `kernel/<soc>/`、每算子的二进制信息 json 进 `kernel/config/<soc>/`。最后 [func.cmake:L502-L522](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/func.cmake#L502-L522) 用 `ascendc_ops_config.py -p ${BIN_OUT_DIR} -s ${BINARY_COMPUTE_UNIT}` 汇总生成 `binary_info_config.json` 并安装到同一 config 目录——运行期就是靠它在 `kernel/config/<soc>/` 下定位 `kernel/<soc>/` 里的哈希名二进制。kernel 源码与动态实现脚本的安装（`IMPL_INSTALL_DIR = .../tbe/${VENDOR_NAME}_impl`，见 [config.cmake:L75-L76](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/config.cmake#L75-L76)）在 [CMakeLists.txt:L665-L698](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L665-L698)。

**（4）run 包打包：CPack + makeself**

[CMakeLists.txt:L736-L780](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L736-L780) 完成三件事：

- L737-L751：把 CANN 侧 scripts 拷到构建目录并用 `sed` 把 `install.sh/upgrade.sh` 里的 `vendor_name=customize` 替换成 `omni_custom_transformer`，随包安装——所以 run 包的安装脚本也是**构建产物**（u1-l2 的结论在这里落到源码行）。
- L753-L766：`gen_version_info.sh` 生成 `version.info` 并装到 `vendors/${VENDOR_NAME}/` 根（正是 4.2.3 版本校验读取的文件）。
- L768-L780：CPack 配置——包名 `CANN-omni_custom_ops-${CANN_VERSION}-linux.${CMAKE_SYSTEM_PROCESSOR}.run`，`CPACK_GENERATOR External` + `CPACK_EXTERNAL_PACKAGE_SCRIPT makeself.cmake`，即用 makeself 生成自解压脚本。

成功标志与产物位置见 [ascendc/README.md:L275-L279](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/README.md#L275-L279)：`output/` 目录下出现 `CANN-omni_custom_ops-<cann_version>-linux.<arch>.run`。

**（5）安装与 set_env.bash**

[ascendc/README.md:L286-L292](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/README.md#L286-L292) 给出标准安装三步：

```bash
chmod +x CANN-omni_custom_ops-<cann_version>-linux.<arch>.run
./CANN-omni_custom_ops-<cann_version>-linux.<arch>.run --quiet --install-path=/usr/local/Ascend/ascend-toolkit/latest/opp
source /usr/local/Ascend/ascend-toolkit/latest/opp/vendors/omni_custom_transformer/bin/set_env.bash
```

安装路径选 toolkit 的 `opp/`，run 包把 `packages/vendors/omni_custom_transformer/` 整树释放成 `opp/vendors/omni_custom_transformer/`，并由（sed 改过名的）安装脚本生成 `bin/set_env.bash`——该文件在仓库里**不存在**，是安装期产物；source 它之后 `ASCEND_OPP_PATH/LD_LIBRARY_PATH` 等环境变量指向自定义算子目录。这正是 u3-l2 讲过的 dlopen 六级查找链里「自定义 vendors 优先于 CANN 内置库」能成立的前提。

**（6）wheel 包收尾**

[ascendc/torch_ops_extension/build_and_install.sh:L13-L21](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/build_and_install.sh#L13-L21) 三步走：清 `build/` → `python3 setup.py build bdist_wheel` → `pip3 install *.whl --force-reinstall`。[setup.py:L49-L67](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/setup.py#L49-L67) 中两条 glob 自动收集 csrc 源码、`NpuExtension` 编成单个 `custom_ops_lib`、包名 `omni_custom_ops` 版本 `1.0`（细节 u1-l4/u3-l4 已讲）。wheel 与 run 包的缝合点仍是 L3 的 `dlopen/dlsym`——**先 run 后 wheel** 的顺序约束由此而来。

#### 4.3.4 代码实践

**实践目标**：在无硬件环境下，手工「纸面安装」一遍 run 包——根据 CMake install 规则推演 vendors 目录树。

**操作步骤**：

1. 通读 [CMakeLists.txt:L108-L274](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L108-L274) 与 [func.cmake:L422-L428](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/func.cmake#L422-L428) 的每一条 `install()`，把目标产物填进下面的树：

```text
opp/vendors/omni_custom_transformer/
├── op_api/lib/libcust_opapi.so
├── op_api/include/aclnn_*.h
├── op_proto/lib/linux/<arch>/libcust_opsproto_rt2.0.so
├── op_proto/inc/*_proto.h
├── op_impl/ai_core/tbe/
│   ├── op_tiling/liboptiling.so                （兼容软链）
│   ├── op_tiling/lib/linux/<arch>/libcust_opmaster_rt2.0.so
│   ├── kernel/<soc>/...                        （哈希名 kernel 二进制）
│   ├── kernel/config/<soc>/binary_info_config.json、<op>.json
│   └── omni_custom_transformer_impl/ascendc/... （kernel 源码与实现脚本）
├── version.info
└── bin/set_env.bash                            （安装期生成）
```

2. （有环境时）真机安装 run 包后用 `tree -L 6 /usr/local/Ascend/ascend-toolkit/latest/opp/vendors/omni_custom_transformer`（或 `find ... -maxdepth 6`）对照你画的树，逐项打勾。
3. `cat` 安装后的 `bin/set_env.bash`，列出它 export 了哪些环境变量。

**需要观察的现象**：纸面树与实际安装树逐项吻合；`set_env.bash` 至少注入了 `LD_LIBRARY_PATH` 与指向该 vendors 目录的 `ASCEND_OPP_PATH`（或等价变量）。

**预期结果**：若发现实际树多了/少了某个叶子，回到对应 `install()` 规则核对是哪个目标所为。步骤 2-3 **待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`liboptiling.so` 与 `libcust_opmaster_rt2.0.so` 是两个库吗？

**参考答案**：不是。`optiling` 目标的真实输出名被改写为 `cust_opmaster_rt2.0`（CMakeLists L247-L249）；`liboptiling.so` 只是安装在 `op_tiling/` 下一级、指向 `lib/linux/<arch>/libcust_opmaster_rt2.0.so` 的兼容软链（L259-L274），服务按旧路径查找 tiling 库的运行时。

**练习 2**：为什么 kernel 二进制要放在 `kernel/<soc>/`、配置放在 `kernel/config/<soc>/`，都按 SOC 分目录？

**参考答案**：一次构建可用 `-c "a;b"` 编多个 SOC（`foreach (compute_unit ${ASCEND_COMPUTE_UNIT})`），每个 SOC 各产出一份二进制与一份 `binary_info_config.json`；运行期设备上报具体芯片型号后，框架到对应的 `<soc>` 子目录检索，互不干扰。4.1.3 中 matmul 知识库文件名精确到 `B1_24/B3_20` 也是同样的道理——芯片参数不同，最优产物不同。

**练习 3**：用户 source 了 toolkit 的 `set_env.sh`，却忘了 source vendors 下的 `set_env.bash`，会出现什么现象？

**参考答案**：自定义算子目录未注入查找路径：torch 侧 `import omni_custom_ops` 后调用算子时，`EXEC_NPU_CMD_V1` 在六级 dlopen 链里找不到 `libcust_opapi.so` 里的 aclnn 符号，触发 `TORCH_CHECK` 报「符号找不到」类错误——即 u3-l4 排查五环节中的第 2 环（`ASCEND_OPP_PATH` 未指向自定义 vendors）。

## 5. 综合实践

**任务**：产出两份交付物——「完整发布流程图」与「新 SOC 适配清单」。这是本讲的毕业练习，也是把 u1-l2 → u5-l1 → 本讲串起来的总装。

**交付物一：完整发布流程图**。以 `bash build.sh -c ascend910_93 -n 'ai_infra_moe_init_routing_v3'` 为例，画出从命令行到 `torch.ops.custom` 可调用的全流程，每个环节标注：所在文件与关键行号、输入、输出产物及其路径。至少覆盖：

1. 参数解析（build.sh:L262-L353）与 CMake 变量注入（L358-L433）。
2. 配置期版本校验开关（config.cmake:L175-L194）与 prepare.sh 预生成（L213-L232）。
3. 三大库目标 opapi/opsproto/optiling 的编译与 install 规则（CMakeLists:L108-L274）。
4. 按 SOC 的二进制编译与 `binary_info_config.json` 生成（CMakeLists:L721-L734、func.cmake:L422-L522）。
5. CPack/makeself 打出 run 包（CMakeLists:L768-L780）→ `output/CANN-omni_custom_ops-*.run`。
6. 安装到 `opp/vendors/omni_custom_transformer/` 并 source `bin/set_env.bash`（README:L286-L292）。
7. wheel 打包安装（build_and_install.sh:L13-L21）→ `import omni_custom_ops` 验证。

**交付物二：新 SOC 适配清单**。假设要适配新芯片 `ascend999`（示例代号），对照本讲源码写出文件级清单：

| 序号 | 需要做的事 | 参照源码 |
| --- | --- | --- |
| 1 | 每个 def 文件追加 `AddConfig("ascend999")`；IO 规格不同则仿照 `config_950` 另写一份 `OpAICoreConfig` 并加宏守卫 | kv_rms_norm_rope_cache_def.cpp:L254-L356 |
| 2 | 算子级 CMakeLists 增加新 SOC 的宏守卫（如需独立原型） | kv_rms_norm_rope_cache/CMakeLists.txt:L17-L20 |
| 3 | 需要预编译裁剪/离线调优的算子，新增 `op_host/config/ascend999/` 下的 binary.json、simplified_key.ini，matmul 还需 `*_runtime_kb.json`（文件名带型号与核数） | moe config 目录；matmul config 目录 |
| 4 | kernel/tiling 侧新增架构分支（如 arch35 之于 950），host 侧决策树与新 TilingKey 对齐，UT 断言双侧 key 镜像 | 参照 u4-l7 arch35、u5-l1 模板链 |
| 5 | 芯片 Docker 镜像与 CANN 包配套（README 的 A2/A3/A5 镜像清单即三类 SOC 的环境对应物） | README:L202-L207 |
| 6 | 编译验证：`bash build.sh -c ascend999`，产物 run 包安装后跑 ST 对拍 | README:L248-L301 |

**验收**（有环境时）：流程图中每个环节的产物路径能在真实构建/安装输出中找到；无环境时以「能对着源码行号讲出每个环节的因果链」为验收标准。

## 6. 本讲小结

- **SOC 适配三层落点**：OpDef `AddConfig` 是准入声明（950 用独立 `OpAICoreConfig` + `USE_ASCEND950` 宏守卫，由算子级 CMakeLists 在 `-c ascend950` 时注入）；`op_host/config/<soc>/` 按需存放 binary.json（预编译 dtype 组合清单，bin_filename 带内容哈希）、simplified_key.ini（opc `--simplified_key_mode`）、runtime_kb.json（matmul 的「形状指纹→调优参数」离线知识库，精确到型号与核数）；kernel/tiling 的架构特化分支是第三层。
- **`--tiling_key` 是六级传递链**：build.sh 解析 → `-DTILING_KEY` → config.cmake 转义为 `::` 传 prepare.sh → 还原后注入内层 CMake → `add_ops_tiling_keys` 落盘 `custom_tiling_keys.ini`（或 `--tiling_key=` 编译选项）→ opc 按 key 裁剪二进制（`TILINGKEY_PAR_COMPILE=1` 并行）。它只裁二进制、不裁 host 逻辑，被裁掉的 key 运行期无法执行。
- **`--disable-check-compatible` 的代码事实**：build.sh 预定义与该开关都置 `CHECK_COMPATIBLE="false"`，最终 `-DCHECK_COMPATIBLE` 控制 config.cmake 是否跑 `check_version_compatible.py`（读 version.info 与 CANN 包比对）；当前仓库默认即关闭，README 的 FAQ 是针对校验开启场景的解法。
- **安装布局可以精确到叶子**：三大库分别落在 `op_api/lib`、`op_proto/lib/linux/<arch>`、`op_impl/ai_core/tbe/op_tiling/lib/linux/<arch>`（外加 `liboptiling.so` 兼容软链）；kernel 二进制与 `binary_info_config.json` 按 `kernel/<soc>/`、`kernel/config/<soc>/` 分目录；`install.sh`（sed 替换 vendor_name）、`version.info`、安装后的 `bin/set_env.bash` 都是构建/安装期产物。
- **发布链完整闭环**：CPack(External+makeself) 打出 `CANN-omni_custom_ops-*.run` → 安装释放到 `opp/vendors/omni_custom_transformer/` 并 source `set_env.bash` 注入环境 → `build_and_install.sh` 打出并安装 `omni_custom_ops` wheel → torch 侧经 dlopen 缝合两包；顺序必须先 run 后 wheel。

## 7. 下一步学习建议

- 最后一讲 **u6-l3 综合实战：从零新增一个自定义推理算子**（若尚未完成）是本讲知识的反向运用——本讲讲「一套算子库如何适配多芯片并发布」，实战讲「一个新算子如何进入这套体系」；两讲合看即完整的二次开发视野。
- 深入 CANN 侧构建细节：`prepare_build` 生成的 per-op 编译脚本与 `custom_tiling_keys.ini` 的消费逻辑位于 CANN 安装目录（`${ASCEND_CANN_PACKAGE_PATH}` 下的 cmake/脚本），可对照本讲 4.2 的传递链去安装目录里找 `--tiling_key` 的最终消费点。
- 回顾 u4-l8（matmul 的 simplified_key 与 runtime_kb 的运行期消费）、u5-l1（TilingKey 编码体系）与 u3-l2（dlopen 六级查找链），把「编译期配置 → 运行期检索」的两端在本讲建立的安装布局上对齐。
- 若有真实多芯片环境，建议对同一算子分别在 910b/910_93/950 上构建 run 包并对比 `kernel/<soc>/` 产物差异，直观感受「一份源码 × N 份二进制」。
