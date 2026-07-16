# 代码检查与综合测试自动化

## 1. 本讲目标

Open Logic 把“可信代码”（Trustable Code）作为第一设计哲学（见 u1-l1）。可信不是口号，而是两条自动化流水线在背后兜底：

- **静态检查（Lint）**：在每个 PR 上用 VSG（VHDL Style Guide）扫描全部源码，确保命名、缩进、握手约定（见 u1-l5）被一致遵守。
- **综合测试（Inference Test）**：把每个实体用多组泛型喂给 6 种厂商工具做综合，验证“合法 VHDL 在真实器件上确实能被综合、能推断出期望资源”。

学完本讲，你应当能够：

1. 说清 `lint/script/script.py` 如何把全仓库 VHDL 分成“生产代码”和“VC 代码”两批，分别套用不同 VSG 配置。
2. 读懂 `vsg_config.yml` 与 `vsg_config_overlay_vc.yml` 的覆盖关系，解释为什么 VC（验证组件）要放宽后缀与行长规则。
3. 描述 `tools/inference_test` 的整体架构：YAML → 解析实体 → Jinja2 生成顶层包装 → 调用厂商工具 → 解析报告 → 扣减 I/O 归约资源。
4. 在 `yaml/base.yml` 中为一个新实体新增综合测试条目，并理解 `--check-coverage` / `--dry-run` 在免费 CI 与付费 CI 中的分工。

## 2. 前置知识

本讲依赖 u1-l5 建立的编码规范认知（`_g` 泛型后缀、`_c` 常量后缀、AXI-S 握手、两进程法），并承接 u10-l1（VC 命名约定 `olo_test_*_vc`）与 u10-l2/u10-l3 的 CI/质量闭环主题。下面补充几个本讲专用术语：

- **Lint（静态检查）**：不运行代码、不综合，仅按规则扫描源码文本，报告风格与潜在问题。在 CI 里作为“格式化门禁”——有 error/warning 就拒绝合并。
- **VSG（VHDL Style Guide）**：一个开源的 VHDL 风格检查器（Python 实现），用一份 YAML 配置描述成百上千条规则（每条规则控制缩进、空格、大小写、后缀等的一个细节）。
- **Configuration 叠加**：VSG 允许在命令行用多个 `-c` 依次传入多份配置文件，**后者的同名规则覆盖前者**。本讲把这套机制用来给 VC 单独“开小灶”。
- **综合（Synthesis）**：把 RTL（寄存器传输级 VHDL）翻译成特定 FPGA 厂商的网表，并报告用了多少 LUT/寄存器/RAM/DSP。
- **Inference（推断）**：综合工具识别出某段 RTL “应该”映射成块 RAM、DSP 块等专用硬件的过程。Open Logic 的 RAM 实体就靠 RTL 写法引导工具正确推断（见 u2-l3）。
- **DUT（Device Under Test）**：被测实体。综合测试里指真正要评估资源的那一个 Open Logic 实体。
- **Jinja2 模板**：Python 的文本模板引擎，用 `{% for %}` 等标签把数据渲染成 VHDL/TCL 文本。本讲中 `olo_fix_pkg_writer`（u8-l4）和 inference_test 都用它做代码生成。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lint/script/script.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/script/script.py) | Lint 驱动脚本：发现并分类 VHDL 文件，分批调用 VSG，汇总失败状态。 |
| [lint/config/vsg_config.yml](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/config/vsg_config.yml) | 生产代码的 VSG 主配置（4500+ 行规则）。 |
| [lint/config/vsg_config_overlay_vc.yml](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/config/vsg_config_overlay_vc.yml) | 叠加在主配置之上、专给 VC 放宽规则的覆盖配置。 |
| [tools/inference_test/InferenceTest.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/InferenceTest.py) | 综合测试主入口：解析 CLI、解析 YAML、循环“实体×工具×配置”跑综合并落盘资源表。 |
| [tools/inference_test/YamlInterpreter.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/YamlInterpreter.py) | 解析 YAML，按 include/exclude 匹配源文件，构造 `TopLevel` 对象。 |
| [tools/inference_test/TopLevel.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/TopLevel.py) | 用 Jinja2 渲染 `top.template` 生成综合顶层 `test.vhd`，含 I/O 归约。 |
| [tools/inference_test/EntityCollection.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/EntityCollection.py) | 用正则解析 Open Logic 实体的泛型/端口（仅适配 OLO 规范写法）。 |
| [tools/inference_test/yaml/base.yml](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/yaml/base.yml) | base 区域综合测试配置：每个实体配几组泛型。 |
| [tools/inference_test/vhdl/in_reduce.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/vhdl/in_reduce.vhd) | 输入归约：把多个 DUT 输入经移位寄存器收成 2 根 I/O，防止被优化掉。 |
| [.github/workflows/hdl_check.yml](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/hdl_check.yml) | 免费 CI：跑 Lint + 综合 YAML 覆盖检查（`--dry-run`）。 |

## 4. 核心概念与源码讲解

### 4.1 VSG 检查与配置

#### 4.1.1 概念说明

VSG（VHDL Style Guide）是一个把“编码规范”变成“可执行检查”的工具。u1-l5 讲了 Open Logic 的命名/握手/复位规范——如果没有工具强制，规范就会在 PR review 里靠人眼逐步退化。VSG 的做法是：用一份 YAML 把几百条规则（每条管一个细节，比如“泛型必须以 `_g` 结尾”）描述出来，然后命令行一跑，不符合就报 error。

Open Logic 的主配置 [`lint/config/vsg_config.yml`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/config/vsg_config.yml) 文件头说明它兼容 VSG 3.25.0，并给出了标准用法（[lint/config/vsg_config.yml:9-16](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/config/vsg_config.yml#L9-L16)）。配置分三段：

1. **indent**：每种 VHDL 语法结构（`if`、`case`、`process`、`port_map` 等）的缩进令牌。
2. **pragma**：识别综合 pragma（如 `-- synthesis translate_off`），避免把综合指令当普通注释检查。
3. **rule**：真正的一条条规则定义。

#### 4.1.2 核心流程

`global` 段是所有规则的默认基线（[lint/config/vsg_config.yml:586-592](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/config/vsg_config.yml#L586-L592)）：

```yaml
global:
    disable: false      # 默认启用全部规则
    fixable: true       # 默认可自动修复
    severity: Error     # 默认严重级别为 Error
    indent_size: 4
    indent_style: spaces
```

关键点是 `severity: Error`——Open Logic 选择把风格问题当作 **Error 而非 Warning**。这意味着 PR 里只要有一条规则不过，CI 直接红，必须改到通过才能合并。文件里大量规则的注释写着 `# Could be Warning for Development`，说明作者考虑过降级，但生产中保持 Error 以严守规范。

几条直接呼应 u1-l5 编码规范的代表性规则：

| 规则 | 配置 | 对应规范 |
| --- | --- | --- |
| `generic_600`（[L2323-L2327](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/config/vsg_config.yml#L2323-L2327)） | `suffixes: ["_g"]`，例外 `["tb_path","output_path","runner_cfg"]` | 泛型后缀 `_g` |
| `constant_600`（[L1618-L1622](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/config/vsg_config.yml#L1618-L1622)） | `suffixes: ["_c"]` | 常量后缀 `_c` |
| `architecture_025`（[L788-L792](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/config/vsg_config.yml#L788-L792)） | `names: ["rtl","struct","sim","mdl"]` | 架构名只能是这四个 |
| `function_017`（[L2024-L2030](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/config/vsg_config.yml#L2024-L2030)） | `case: camelCase` | 函数名用驼峰（如 `binaryToGray`） |

注意 `generic_600` 的例外列表里特意放了 `tb_path`/`output_path`/`runner_cfg`——这三个不带 `_g` 的泛型是 **VUnit 测试台固定的接口约定**（见 u10-l1），不能改名，所以白名单放行。

VSG 的规则按 **phase（阶段）** 组织（1~7），`--all_phases` 让它跑完所有阶段。各阶段大致分工：phase 1 结构、phase 2 空格、phase 3 空行、phase 4 缩进、phase 5 对齐、phase 6 大小写、phase 7 前后缀。命名相关规则多在 phase 6/7，所以必须用 `--all_phases` 才会真正检查后缀。

#### 4.1.3 源码精读

驱动脚本 [`lint/script/script.py`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/script/script.py) 非常短（约 125 行），逻辑分四步。

**第一步：定位与排除名单。** 脚本启动时先切到自己所在目录（[lint/script/script.py:7](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/script/script.py#L7)），再把搜索根设为 `../..`（仓库根），并定义两个排除名单（[lint/script/script.py:20-21](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/script/script.py#L20-L21)）：

```python
NOT_LINTED = ["RbExample.vhd"]      # 文档里的示例，本身是不完整 VHDL
NOT_LINTED_DIR = ["../../3rdParty/"] # 第三方库（如 en_cl_fix）
```

`RbExample.vhd` 是 AXI4-Lite 从机文档里的教学片段（见 u6-l2），刻意不完整，所以跳过；`3rdParty/` 是 MIT 许可的 en_cl_fix（见 u1-l3），不属于 Open Logic 自己的代码，也不查。

**第二步：跨平台分块。** Windows 命令行长度上限 8192 字符，一次塞太多文件路径会超限，所以按 30 个文件一组切块；Linux 无此限制，一次性传全部以提速（[lint/script/script.py:27-33](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/script/script.py#L27-L33)）。

**第三步：把文件分成两批。** 这是本讲最关键的设计——按目录位置区分“生产代码”与“VC 代码”：

```python
def root_is_vc(root):
    return root.name == 'tb' and root.parent.name == 'test'   # 即 test/tb/*.vhd
```

（[lint/script/script.py:38-39](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/script/script.py#L38-L39)）

`find_normal_vhd_files` 收集**除** `test/tb/` 与排除名单外的所有 `.vhd`（[lint/script/script.py:41-59](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/script/script.py#L41-L59)）；`find_vc_vhd_files` 则**只**收 `test/tb/` 下的文件（[lint/script/script.py:61-68](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/script/script.py#L61-L68)）。两者用的是同一棵 `rglob('*.vhd')` 树，只是过滤方向相反，互不重叠。这也意味着：**一个文件是不是 VC，完全由它放在 `test/tb/` 决定**，与文件名无关——这正对应 u10-l1 的“VC 置于 `test/tb/`、命名 `olo_test_*_vc`”约定。

**第四步：分批调用 VSG。** 两批文件用**不同的配置组合**调用 VSG（[lint/script/script.py:98-116](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/script/script.py#L98-L116)）：

```python
# 生产代码：只套主配置
vsg -c ../config/vsg_config.yml -f <all_files> --junit ../report/vsg_normal_vhdl.xml --all_phases -of vsg
# VC 代码：主配置 + 覆盖配置（叠加）
vsg -c ../config/vsg_config.yml ../config/vsg_config_overlay_vc.yml -f <all_files> --junit ../report/vsg_vc_vhdl.xml --all_phases -of vsg
```

两条命令都带 `--junit` 把结果写成 XML 报告（分别落到 `report/vsg_normal_vhdl.xml` 和 `report/vsg_vc_vhdl.xml`，这两个文件被 `.gitignore` 忽略、作为 CI 构件上传），并以 `--all_phases` 跑全阶段。`-of vsg` 是输出格式（也支持 `-of syntastic` 供编辑器集成，由 `--syntastic` 开关切换）。任一批返回非零退出码，脚本最终 `raise Exception`（[lint/script/script.py:118-119](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/script/script.py#L118-L119)）让 CI 失败。`--debug` 模式则逐文件检查、遇错即停，便于本地定位。

#### 4.1.4 代码实践

1. **目标**：亲手跑一次 Lint，确认本地源码 0 error。
2. **操作步骤**：
   - 安装 VSG：`pip install vsg`（确认版本与配置要求的 3.25.0 兼容）。
   - 在仓库根执行 `cd lint/script && python3 script.py`。
3. **观察现象**：脚本会先打印 `Normal VHDL Files` 与 `VC VHDL Files` 两个文件清单，然后逐块打印 `Start Linting`。全绿时最后一行是 `All VHDL files linted successfully`。
4. **预期结果**：在干净的 HEAD 上，两批文件均应 0 error 通过。
5. **待本地验证**：若你的 VSG 版本与 3.25.0 差异较大，可能个别规则名变化导致报错——以本地实际输出为准。

### 4.2 VC 与生产代码的区分

#### 4.2.1 概念说明

为什么要把 VC（Verification Component，验证组件，见 u10-l1）单独拎出来？因为 VC 不是“要被综合进 FPGA 的生产代码”，而是“跑在仿真器里的总线功能模型”。它有两个与生产代码冲突的特点：

1. **它要对接 VUnit 原生 VC 的接口风格**——而 VUnit 的接口、消息机制代码本身就长、且不遵守 Open Logic 的 `_g`/`_c` 后缀约定。
2. **它常常很长**——一个 VC 可能把整套 AXI 主机行为模型塞在一个文件里。

如果硬把生产代码的规则套到 VC 上，要么改不动（VUnit 接口名不能改），要么把一个长文件拆成一堆碎片文件（违背“一个 VC 做一件事”）。所以 Open Logic 用一份**覆盖配置**给 VC 放宽规则。

#### 4.2.2 核心流程

[`lint/config/vsg_config_overlay_vc.yml`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/config/vsg_config_overlay_vc.yml) 文件头一句话点明意图（[L1](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/config/vsg_config_overlay_vc.yml#L1)）：

> Rule overlay to match VUnit interface style for verification components (VCs)

VSG 的多配置叠加规则是“**后传入的同名规则覆盖先传入的**”。脚本里 VC 批的命令是 `-c vsg_config.yml vsg_config_overlay_vc.yml`，所以 overlay 里出现的任何规则都会盖掉主配置的同名规则。覆盖配置里绝大多数条目是把 `*_500`/`*_501` 等“大小写”规则重申为 `case: lower`（与主配置一致，属于显式声明，确保覆盖时大小写检查仍生效），真正“放宽”的是这几条禁用项：

| 覆盖条目 | 主配置原值 | overlay 改成 | 原因 |
| --- | --- | --- | --- |
| `generic_600`（[L182-L183](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/config/vsg_config_overlay_vc.yml#L182-L183)） | `suffixes: ["_g"]` | `disable: true` | VC 泛型对接 VUnit，不强求 `_g` |
| `constant_600`（[L104-L106](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/config/vsg_config_overlay_vc.yml#L104-L106)） | `suffixes: ["_c"]` | `disable: true` | 同上，常量后缀放宽 |
| `variable_600`（[L373-L374](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/config/vsg_config_overlay_vc.yml#L373-L374)） | 变量后缀检查 | `disable: true` | VC 用大量局部变量 |
| `length_002`（[L224-L225](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/config/vsg_config_overlay_vc.yml#L224-L225)） | 限制行长 | `disable: true` | 注释原文：`Better long VC files than many short ones` |
| `architecture_025`（[L25-L26](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/config/vsg_config_overlay_vc.yml#L25-L26)） | `names: ["rtl","struct","sim","mdl"]` | `names: ["a"]` | VC 架构名用 VUnit 惯用的 `a` |

注意 `length_002` 那行注释是整个设计取舍的精华：**宁可让一个 VC 文件变长，也不把它拆成多个短文件**——因为拆文件会破坏“一个 VC = 一个可复用总线模型”的内聚性。

#### 4.2.3 源码精读

覆盖关系之所以能成立，全靠脚本里那条“主配置 + overlay”的命令（[lint/script/script.py:114](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/script/script.py#L114)）。也就是说，区分 VC 与生产代码的“开关”不在 VSG 工具里，而在 **Open Logic 自己的脚本逻辑**：脚本负责把文件按目录路由到正确的配置组合，VSG 本身只看到最终生效的那份合并配置。

这套设计的附带好处是可演进：将来若某条规则对生产代码也太严，可以新建一份 `vsg_config_overlay_xxx.yml` 并在脚本里再加一路分支，主配置保持稳定。

#### 4.2.4 代码实践

1. **目标**：亲眼看到 overlay 改变了同一规则的判定。
2. **操作步骤**：
   - 在 `test/tb/` 下随便挑一个 `olo_test_*_vc.vhd`，例如用 `vsg -c lint/config/vsg_config.yml -f <vc文件>` **只**套主配置运行，观察 `generic_600`/`length_002` 是否报错。
   - 再用 `vsg -c lint/config/vsg_config.yml lint/config/vsg_config_overlay_vc.yml -f <同一文件>` 套叠加配置运行。
3. **观察现象**：第一次会冒出后缀/行长相关的 error；第二次这些 error 消失。
4. **预期结果**：叠加配置后该 VC 应通过（或仅剩与 overlay 无关的少量问题）。
5. **待本地验证**：具体报错条数取决于所选 VC 文件，以本地输出为准。

### 4.3 inference_test 框架

#### 4.3.1 概念说明

Lint 只能保证代码“写得规范”，但不能保证“能在真实 FPGA 上综合出来”。问题在于：**合法的 VHDL 不等于所有工具都能综合**——某些工具会对完全合法的语句报错，或把它综合成意料之外的资源（比如本该是块 RAM 却退化成触发器）。Open Logic 自称“厂商无关、可跑于任意 FPGA”（见 u1-l1），这一承诺必须被自动化验证，否则就是空话。

`tools/inference_test` 就是这套验证框架。它的官方文档 [`inference_test.md`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/inference_test.md) 开宗明义（[L5-L12](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/inference_test.md#L5-L12)）：目标是“对多个实体 × 多组泛型 × 多种工具跑综合”，既检查综合能否成功，又比较不同泛型组合的资源用量、发现异常数字。

#### 4.3.2 核心流程

整个框架的运转可以用一条流水线概括：

```
YAML(base.yml等)
   │  YamlInterpreter 解析 + glob 匹配源文件
   ▼
EntityCollection: 用正则从 .vhd 提取每个实体的 泛型/端口
   │  (--check-coverage: 校验所有实体都被覆盖)
   ▼
对每个 (实体, 工具, 配置):
   TopLevel.create_syn_file()  ──Jinja2 渲染 top.template──▶ test.vhd  (顶层包装 + I/O 归约)
   tool.sythesize()            ──调用厂商工具批量综合──────▶ utilization 报告
   tool.get_resource_usage()   ──解析报告──▶ LUT/Reg/RAM/DSP
   减去 in_reduce/out_reduce 资源 ──▶ DUT 真实资源
   tool.check_drc()            ──检查 latch 等 DRC 违例
   ▼
ResourceResults → PrettyTable → 写入 results/<yml>.txt
```

**六个工具**在主入口里以字典注册（[InferenceTest.py:79-84](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/InferenceTest.py#L79-L84)）：`quartus`、`vivado`、`gowin`、`efinity`、`libero`、`cologne`（Cologne Chip）。`--tool vivado,quartus` 可只跑子集。所有工具继承自 [`ToolBase`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/ToolBase.py)，统一接口 `synthesize()` / `get_resource_usage()` / `get_in_reduce_resources()` / `get_out_reduce_resources()` / `check_drc()`（[ToolBase.py:55-87](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/ToolBase.py#L55-L87)）。

**三层循环**是主逻辑骨架（[InferenceTest.py:114-164](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/InferenceTest.py#L114-L164)）：外层遍历实体、中层遍历工具、内层遍历该实体的配置。每个配置先 `create_syn_file()` 生成顶层（[L138](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/InferenceTest.py#L138)），再 `tool.sythesize()` 综合以 `test` 为顶层（[L141](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/InferenceTest.py#L141)）。注意综合传入的文件列表是 `[IN_REDUCE_FILE, OUT_REDUCE_FILE, SYN_FILE]`——也就是说综合顶层 `test` 内部会实例化 DUT + 输入归约 + 输出归约三部分（见 4.3.3 的模板）。

**资源扣减**是这套框架最精巧的一点（[InferenceTest.py:143-148](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/InferenceTest.py#L143-L148)）：

```python
resources_measured = tool.get_resource_usage()
resources_total = {k: resources_measured[k]
                     - tool.get_in_reduce_resources(in_red_size)[k]
                     - tool.get_out_reduce_resources(out_red_size)[k]
                   for k in resources_measured}
resources_total = {k: max(0.0, v) for k, v in resources_total.items()}
```

因为综合的是“包装后”的顶层，报告里的资源包含了归约逻辑的开销，必须把它们减掉才得到 DUT 本身的真实资源。`max(0.0, v)` 是兜底：扣减模型是近似值，偶尔算出 −1 也钳到 0（注释说明源于输入/输出归约校正不精确）。

#### 4.3.3 源码精读

**为什么需要顶层包装？** 文档讲得很直白（[inference_test.md:130-141](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/inference_test.md#L130-L141)）：不是所有工具都允许给顶层实体设泛型，而且很多 Open Logic 实体的泛型没有默认值，直接当顶层会综合失败。所以框架自动生成一个 `test.vhd` 包装器，在里面把所有泛型赋好值，并顺带做 I/O 归约。

**实体解析器** [`EntityCollection.py`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/EntityCollection.py) 是一个**有意简化的**的正则解析器，文件头明确警告（[EntityCollection.py:8-9](https://github.com/open-logic/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/EntityCollection.py#L8-L9)）：

> Only works for VHDL strictly according to open-logic coding guidelines. The parser is not meant to cover VHDL outside of open-logic guidelines.

它靠 `^entity ... ^end ...;` 的正则切块（[EntityCollection.py:86](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/EntityCollection.py#L86)），再逐行 `split(":")` 解析泛型/端口。这之所以能工作，正因为 u1-l5 规范要求“一行一个声明、冒号分隔”——规范不只是好看，它让这种轻量解析成为可能。

**顶层模板** [`top.template`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/top.template)（Jinja2）把上面解析到的泛型/端口渲染成一个 `entity test`。模板里实例化了三样东西（[top.template:52-103](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/top.template#L52-L103)）：

1. `i_dut : entity olo.<entity_name>`——真正的被测实体，泛型全量映射；
2. `i_ff`——一个单触发器，注释说明“某些工具遇到无逻辑设计会崩溃，所以永远加一个 FF”（[top.template:65-71](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/top.template#L65-L71)）；
3. 可选的 `i_in_reduce` / `i_out_reduce`——I/O 归约逻辑。

**I/O 归约** 解决的是一个很现实的问题：某些工具在“顶层端口数 > 目标器件引脚数”时直接综合失败（[in_reduce.vhd:10-11](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/vhdl/in_reduce.vhd#L10-L11)）。框架的做法不是删端口，而是把多个 DUT 端口接到一根移位寄存器上，对外只露 `Data`+`Latch` 两根 I/O（[in_reduce.vhd:45-53](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/vhdl/in_reduce.vhd#L45-L53)）。关键在于移位寄存器**带时钟反馈**，让工具无法把这些端口优化掉，从而如实报告 DUT 资源。输出侧对称（[out_reduce.vhd:44-56](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/vhdl/out_reduce.vhd#L44-L56)）。

**工具子类**以 [`ToolVivado.py`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/ToolVivado.py) 为例：`synthesize()` 用 `pexpect` 以 batch 模式驱动 `vivado -mode batch -source synthesize.tcl`（[ToolVivado.py:42-48](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/ToolVivado.py#L42-L48)），而那条 TCL 是用 Open Logic 自己的 `import_sources.tcl`（见 u1-l3）把源码导入后综合的——inference_test 复用了厂商集成脚本；`get_resource_usage()` 解析 `utilization_synth.rpt`，按 `|` 分列提取 LUT/Reg/BRAM/DSP（[ToolVivado.py:64-87](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/ToolVivado.py#L64-L87)）；`check_drc()` 扫描日志里是否出现 `inferring latch`（[ToolVivado.py:105-111](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/ToolVivado.py#L105-L111)）——锁存器是 RTL 写错的典型信号，必须拦截。

#### 4.3.4 代码实践

1. **目标**：不花一分钱、不装任何厂商工具，先把 inference_test 跑起来，理解它在 CI 里“免费那一档”做的事。
2. **操作步骤**：
   - `cd tools/inference_test`
   - `python3 -u ./InferenceTest.py --yml=./yaml/base.yml --no-tables --check-coverage --dry-run`
3. **观察现象**：`--dry-run` 让框架**跳过真正的综合**（[InferenceTest.py:34-35](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/InferenceTest.py#L34-L35)），但仍会解析 YAML、匹配文件、解析实体、做覆盖检查、生成 `test.vhd`。你会看到逐实体逐配置的进度，并生成 `results/base.txt`（只含版本信息头，无资源表）。
4. **预期结果**：`--check-coverage` 通过（即所有实体都被某条 YAML 条目覆盖），最后打印 `*** Done ***`。
5. **待本地验证**：若覆盖检查失败，脚本会列出未覆盖实体名并 `exit(1)`——这通常意味着你新加了实体却忘了在 YAML 里登记（见 4.4 的实践正是补这一步）。

### 4.4 YAML 综合配置

#### 4.4.1 概念说明

框架知道“要测哪些实体、用哪些泛型组合、对哪些工具跑”，全靠一份 YAML。Open Logic 按区域分文件：`yaml/base.yml`、`axi.yml`、`intf.yml`、`fix.yml`（外加 `sample.yml` 教学样例）。`--yml` 参数指定用哪一份。这份 YAML 是“综合测试的唯一真相源”——和 u8-l4 的 `olo_fix_pkg_writer` 一样，用声明式数据驱动代码生成。

#### 4.4.2 核心流程

YAML 的三大段（对应 [`YamlInterpreter._parse_base_yaml`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/YamlInterpreter.py#L35-L98) 的解析）：

1. **files**：`include`/`exclude` 是相对 YAML 文件位置的 glob，决定扫描哪些 `.vhd` 来发现实体。例如 `base.yml` 只 include base 区域源码（[base.yml:1-3](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/yaml/base.yml#L1-L3)）。
2. **exclude_entities**：覆盖检查时忽略的实体模式（如 `olo_private_*`，[base.yml:5-6](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/yaml/base.yml#L5-L6)）——私有实体不单独测。
3. **entities**：每个实体一条，可含 `fixed_generics`、`configurations`、`tool_generics`、`tool_omit`（[YamlInterpreter.py:56-86](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/YamlInterpreter.py#L56-L86)）。

一个实体的完整写法如下（以 `olo_base_ram_sdp` 为例，[base.yml:173-199](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/yaml/base.yml#L173-L199)）：

```yaml
- entity_name: "olo_base_ram_sdp"
  fixed_generics:           # 所有配置共用的固定泛型
    Depth_g: 512
    Width_g: 16
    InitString_g: '"0x1234, 0x5678, 0xDEAD, 0xBEEF"'
  configurations:           # 每个配置只覆盖部分泛型
    - name: "NoBe-NoInit"
      generics:
        InitFormat_g: '"NONE"'
        UseByteEnable_g: false
    - name: "Async"
      generics:
        InitFormat_g: '"NONE"'
        UseByteEnable_g: true
        IsAsync_g: true
```

各字段含义（详见 [`TopLevel.add_config`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/TopLevel.py#L61-L82) 文档与 [inference_test.md:103-118](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/inference_test.md#L103-L118)）：

| 字段 | 作用 |
| --- | --- |
| `fixed_generics` | 对该实体所有配置、所有工具都固定取值的泛型，避免重复抄写。 |
| `configurations[].generics` | 仅在该配置生效的泛型覆盖（与 fixed 合并后交给顶层）。 |
| `configurations[].omitted_ports` | 在顶层里**不连**某些端口（如 CAM 的 `Match_Match`），既能看“少连端口”的资源，也能压低 I/O 数。 |
| `configurations[].in_reduce` / `out_reduce` | 把指定端口接到归约移位寄存器，压低对外 I/O 数且防优化。 |
| `tool_generics` | 某工具专用的泛型取值（如某工具不支持某功能时改值）。 |
| `tool_omit` | 对某工具**跳过**该实体，必须给原因字符串（见 `olo_base_ram_tdp` 对 cologne 的 `tool_omit`，[base.yml:250-251](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/yaml/base.yml#L250-L251)，原因“Cologne Chip 不支持真双口”）。 |

> 提示：字符串型泛型（如 `InitFormat_g`）的值要写成带引号的 `'"NONE"'`——外层单引号是 YAML，内层双引号是 VHDL 字符串字面量。这与 u8-l1/l2 讲的 fix 区域字符串泛型模式一脉相承。

#### 4.4.3 源码精读

`YamlInterpreter` 把 YAML 翻译成一组 `TopLevel` 对象（[`get_top_levels`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/YamlInterpreter.py#L129-L152)），并把 `fixed_generics`/`tool_generics`/`tool_omit`/各 `configurations` 分别灌进去。若某实体**没有** `configurations`，框架自动补一个名为 `Default` 的空配置（[YamlInterpreter.py:145-147](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/YamlInterpreter.py#L145-L147)）——这样无泛型组合需求的实体只写一行 `entity_name` 即可（如 `olo_base_cc_pulse`，[base.yml:52-53](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/yaml/base.yml#L52-L53)）。

主入口里的覆盖检查（[InferenceTest.py:59-69](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/InferenceTest.py#L59-L69)）做的是“集合差集”：`扫描到的实体 − 已登记为顶层 − exclude 模式`，剩下的就是“漏测”实体，有任何一个就 `exit(1)`。这正是 CI 能在**不综合**的前提下（配合 `--dry-run`）守住“每个新实体都被登记”的那道关。

**两档 CI 的分工**也由此清晰：

- [`hdl_check.yml`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/hdl_check.yml) 的 `check-synthesis-config` 作业对四个区域各跑一次 `--check-coverage --dry-run`（[hdl_check.yml:108-123](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/hdl_check.yml#L108-L123)），跑在**免费 GitHub runner** 上，每个 PR 都触发——它只验证“YAML 覆盖完整 + 能生成 test.vhd”，不花综合的钱。
- [`synthesis.yml`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/synthesis.yml) 去掉 `--dry-run`，在装满 6 个厂商工具的 **AWS runner** 上真正综合（[synthesis.yml:61-80](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/synthesis.yml#L61-L80)），只在 PR 到 main / push main / 每月 15 号触发——昂贵所以低频。这与 u10-l3 讲的“按成本分级 CI”原则一致。

#### 4.4.4 代码实践

1. **目标**：为一个新实体在 `base.yml` 增加综合测试条目，并验证免费 CI 能发现它。
2. **操作步骤**：
   - 假设你新增了实体 `olo_base_foo`（泛型 `Width_g`，默认 8）。
   - 在 `tools/inference_test/yaml/base.yml` 的 `entities:` 下追加（**示例代码，非仓库原有**）：
     ```yaml
     # olo_base_foo
     - entity_name: "olo_base_foo"
       configurations:
         - name: "16bit"
           generics:
             Width_g: 16
     ```
   - 先不登记，单独运行覆盖检查，观察它是否被列入未覆盖名单：`cd tools/inference_test && python3 -u ./InferenceTest.py --yml=./yaml/base.yml --no-tables --check-coverage --dry-run`。
   - 登记后再跑一次。
3. **观察现象**：未登记时脚本打印 `ERROR: Not all entities are covered by the test!` 并列出 `olo_base_foo`，退出码非 0；登记后该错误消失。
4. **预期结果**：登记后 `--check-coverage` 通过、`test.vhd` 能为该实体生成。
5. **待本地验证**：真正资源数字需在有厂商工具的 AWS runner 上才能得到（见 `synthesis.yml`）；本地 `--dry-run` 只能验证登记与生成。

#### 4.4.5 小练习与答案

**练习 1**：`olo_base_ram_sdp` 的 `fixed_generics` 里设了 `Depth_g: 512`，而各 `configurations` 里没再出现 `Depth_g`。框架最终综合时 `Depth_g` 取多少？为什么这样组织？

> **答案**：取 512。`fixed_generics` 对该实体所有配置都生效，避免在每个 `configurations` 条目里重复抄 `Depth_g`/`Width_g`/`InitString_g`；每个配置只写它要变化的 `InitFormat_g`/`UseByteEnable_g` 等差异项，YAML 更短、差异更醒目。

**练习 2**：为什么 `olo_base_ram_tdp` 要写 `tool_omit: { cologne: "..." }`？如果不写会怎样？

> **答案**：Cologne Chip FPGA 不支持真双口 RAM（TDP）。`tool_omit` 让框架对 cologne 工具跳过该实体并记录原因。不写的话，cologne 综合该实体会失败，整个 `synthesis.yml` 作业报错——而这是器件能力限制、非代码缺陷，故用 `tool_omit` 显式豁免。

**练习 3**：`check-synthesis-config`（免费 CI）和 `synthesis`（AWS CI）调用的命令只差一个 `--dry-run`。请说明这个开关如何把“昂贵低频综合”和“廉价每 PR 检查”解耦。

> **答案**：`--dry-run` 让框架执行除真正综合外的所有步骤（解析 YAML、覆盖检查、生成 test.vhd），所以免费 runner 能在每个 PR 上守住“YAML 覆盖完整且能生成包装”这道关；而去掉 `--dry-run` 才会调用 `tool.sythesize()` 触发真实综合，这只在付费 AWS runner 上低频运行。同一个 `InferenceTest.py`、同一份 YAML，靠一个开关服务两档 CI。

## 5. 综合实践

把本讲两条流水线串起来验证一遍：

1. **挑一个真实实体**：选 `src/base/vhdl/olo_base_arb_rr.vhd`（轮询仲裁器，见 u5-l2）。
2. **Lint 侧**：运行 `cd lint/script && python3 script.py --debug`，确认该生产代码文件单独检查 0 error；再翻 `vsg_config.yml` 找到 `generic_600`，确认它的 `Width_g` 后缀确实被该规则放行。
3. **综合侧**：查看 `yaml/base.yml` 里 `olo_base_arb_rr` 的登记（[base.yml:16-20](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/yaml/base.yml#L16-L20)，配置 `16bit`），运行 `cd tools/inference_test && python3 -u ./InferenceTest.py --yml=./yaml/base.yml --entity=olo_base_arb_rr --check-coverage --dry-run`，确认只针对该实体生成 `test.vhd` 且覆盖检查通过。
4. **对照两档 CI**：打开 `hdl_check.yml` 与 `synthesis.yml`，在表格里记录：哪条作业跑 Lint、哪条跑覆盖检查（dry-run）、哪条跑真实综合，以及各自运行的 runner 类型与触发频率。

> 说明：第 2、3 步可本地完成；真实资源数字（第 3 步若去掉 `--dry-run`）需厂商工具环境，属“待本地验证”。

## 6. 本讲小结

- **Lint 驱动** `lint/script/script.py` 把全仓库 `.vhd` 按 `test/tb/` 路径分成“生产代码”与“VC 代码”两批，分别套用不同 VSG 配置；排除 `RbExample.vhd` 与 `3rdParty/`，并对 Windows 命令行长度做分块。
- **生产代码主配置** `vsg_config.yml` 把风格规则定为 `severity: Error`，强制 `_g`/`_c` 后缀、限定架构名、驼峰函数名等 u1-l5 规范，PR 有 error 即拒合并。
- **VC 覆盖配置** `vsg_config_overlay_vc.yml` 靠“后配置覆盖前配置”给 VC 放宽 `generic_600`/`constant_600`/`variable_600`/`length_002` 并改架构名为 `a`，以匹配 VUnit 接口风格与“宁长勿碎”的取舍。
- **inference_test 框架** 用 YAML 驱动“实体×工具×配置”三维循环，Jinja2 生成带 I/O 归约的顶层包装 `test.vhd`，综合后从报告解析资源并扣减归约开销得到 DUT 真实资源，还用 `check_drc` 拦截锁存器。
- **两档 CI 分工**：免费 `hdl_check.yml` 跑 Lint + `--dry-run --check-coverage`（每 PR）；付费 `synthesis.yml` 在 AWS runner 上跑真实综合（PR 到 main / 月度），用 `--dry-run` 一个开关解耦廉价检查与昂贵综合。

## 7. 下一步学习建议

- 继续阅读 [`tools/inference_test/TopLevel.py`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/TopLevel.py) 与 `top.template`，对照一个生成的 `test.vhd` 读懂 I/O 归约的位拼接细节。
- 阅读 [`tools/inference_test/ToolVivado.py`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/inference_test/ToolVivado.py) 等工具子类，理解不同厂商报告的解析差异，尝试为某厂商工具补一个 `get_in_reduce_resources` 的精确模型。
- 下一讲 u10-l5 将把视角拉高到厂商集成（FuseSoC core 维护）与完整 CI/发布流程，本讲的两条流水线会在那里被放入更大的工程化图景中。
