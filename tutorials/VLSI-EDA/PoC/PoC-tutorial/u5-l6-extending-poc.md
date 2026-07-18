# 扩展 PoC：添加自己的 IP 核与命名空间

## 1. 本讲目标

本讲是专家层收尾，把前几讲散落的「规范」「组织」「可移植」三条线索收拢成一个可操作的能力：**给 PoC 贡献一个新核，甚至一个全新命名空间**。

学完后你应当能够：

- 说出新增一个 IP 核所需的**完整文件清单**（源码、命名空间包、`.files`、测试台、测试台 `.files`），并解释每一类为何必须存在。
- 在已有命名空间的 `<ns>.pkg.vhdl` 中正确**声明一个新 component**，并理解声明与实现的分离。
- 写出一个符合 PoC 规范的**自检式测试台骨架**，并配上一份 `.files` 清单让 pyIPCMI 能把它编译起来。
- 判断自己的新核是否需要走「厂商专用子实体」的可移植路线（承接 u3-l2），还是一份通用 `rtl` 即可。

本讲**不**重复讲授 VHDL 语法本身，默认你已经读过 u1-l4（编码规范）、u3-l1（命名空间包模式）、u3-l2（厂商选择机制）。

---

## 2. 前置知识

在动手之前，先用一句话回顾三个承接点（细节见对应讲义）：

- **编码规范（u1-l4）**：源码统一用 `.vhdl` 后缀；实体名是 `<命名空间>_<功能>` 的蛇形命名；可综合实现用 `architecture rtl`，测试台实体加 `_tb` 后缀、架构名固定 `tb`；每个文件必须有编辑器配置行 + 带分隔线的文档头 + Apache 2.0 许可证块；时序信号必须给初值。
- **命名空间包模式（u3-l1）**：每个命名空间有一份固定名为 `<ns>.pkg.vhdl` 的「根包」，集中声明 component、type、function，**必须先于该命名空间下任何核被编译**；`.files` 清单里要先 include 公共包，再编译根包，最后编译具体核。
- **厂商选择（u3-l2）**：若核需要跨厂商可移植，写一个厂商无关的「包装实体」用 `if generate` 在通用实现与厂商专用子实体间分发，`.files` 再按 `DeviceVendor` 编译期选文件；若只跑通用实现，则一份 `rtl` 足矣。

一个容易被忽略的概念：**component 声明 vs. 实体直接例化**。VHDL 允许两种例化写法——

- 直接例化：`DUT : entity PoC.arith_addw generic map(...) port map(...);`
- 组件例化：先在包里 `component arith_addw is ... end component;`，再 `DUT : arith_addw generic map(...) port map(...);`

PoC 规范明确「优先使用 component 例化」（见下方源码精读），因此**每新增一个核，都要在命名空间根包里补一份 component 声明**——这是本讲的核心动作之一。

> 术语速查：**根包**（`<ns>.pkg.vhdl`）、**component 声明**（在包里声明的「插座」）、**`.files` 清单**（pyIPCMI 消费的编译脚本，非 VHDL）、**DUT**（Design Under Test，被测核）。

---

## 3. 本讲源码地图

本讲以 `arith` 命名空间下的 `arith_addw`（宽位加法器）作为「标准件」范本，对照讲解新增一个核要模仿的全部要素。

| 文件 | 作用 |
|------|------|
| [vhdl_coding.md](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/vhdl_coding.md) | PoC 官方编码规范，规定命名、文件、架构、文档头、风格 |
| [docs/Entity.template](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/docs/Entity.template) | 文档页生成模板，揭示源码与文档的对应关系 |
| [src/arith/arith_addw.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl) | 一个合规核的完整源码范本（文档头 + entity + architecture rtl） |
| [src/arith/arith.pkg.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith.pkg.vhdl) | 命名空间根包范本，演示 component / type / function 的集中声明 |
| [src/arith/arith_addw.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.files) | 单核编译清单范本，演示依赖装配顺序 |
| [tb/arith/arith_addw_tb.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/arith/arith_addw_tb.vhdl) | 自检式测试台范本 |
| [tb/misc/sync/sync_Bits_tb.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/misc/sync/sync_Bits_tb.files) | 测试台编译清单范本（最简形态） |

---

## 4. 核心概念与源码讲解

### 4.1 新核的完整文件结构

#### 4.1.1 概念说明

在 PoC 里「加一个核」远不止写一个 `.vhdl` 文件。因为 PoC 是一个由 pyIPCMI 统一驱动编译的**整库**，任何一个核要能被「找得到、编译得进来、测得过」，必须同时满足三套约束：

1. **VHDL 约束**：核要能通过 `use PoC.<ns>.all` 拉进来，意味着它必须属于某个已编译进 `PoC` 库的命名空间。
2. **编译清单约束**：pyIPCMI 只编译 `.files` 里列出的文件（见 u5-l1），未被任何清单引用的 `.vhdl` 形同不存在。
3. **验证约束**：一个没有测试台的核不会被社区接受，PoC 的测试台是「自检式」的（断言累计到全局状态，见 u4-l2）。

因此，**新增一个核 = 新增/更新一组配套文件**。文件结构分两种情形：

- **情形 A：在已有命名空间下加核**（最常见）。例如在 `arith` 下加 `arith_xxx`。
- **情形 B：新建一个命名空间**再加核。例如全新建 `mylib`。情形 B 是情形 A 的超集——多出一份根包和一个新目录。

#### 4.1.2 核心流程

情形 A（在已有命名空间加核）的最小文件清单：

```
src/<ns>/<ns>_<core>.vhdl        # 新增：核的源码（entity + architecture rtl）
src/<ns>/<ns>_<core>.files       # 新增：单核编译清单
src/<ns>/<ns>.pkg.vhdl           # 更新：在根包里补一个 component 声明
tb/<ns>/<ns>_<core>_tb.vhdl      # 新增：测试台源码（entity_tb + architecture tb）
tb/<ns>/<ns>_<core>_tb.files     # 新增：测试台编译清单
```

情形 B（新建命名空间 `mylib`），在上面基础上**额外**需要：

```
src/mylib/mylib.pkg.vhdl         # 新增：命名空间根包（component/type/function 目录页）
```

> 注意：PoC 没有「根级 `.files`」自动扫描所有命名空间——每个核都通过自己的 `<core>.files` 声明依赖，测试台再通过 `<core>_tb.files` include 这个清单。这套设计让任意一个核都能被**独立**综合/仿真（见 u4-l4 的上下文外综合）。

各文件之间的装配关系（伪代码）：

```
tb/<core>_tb.files
  └─ include src/<ns>/<core>.files          # 先把被测核的依赖全部拉进来
        ├─ include src/common/common.files  #   公共包（utils/config/...）
        ├─ vhdl PoC "src/<ns>/<ns>.pkg.vhdl"#   命名空间根包（含 component 声明）
        └─ vhdl PoC "src/<ns>/<core>.vhdl"  #   被测核本体
  └─ vhdl test "tb/<ns>/<core>_tb.vhdl"     # 再编译测试台本身
```

#### 4.1.3 源码精读

**① 一个合规核长什么样——`arith_addw.vhdl` 的文件骨架**

文件必须以三行编辑器配置开头，紧跟带分隔线的文档头与许可证块——这是规范硬性要求：

[src/arith/arith_addw.vhdl:L1-L44](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl#L1-L44) —— 编辑器配置行（强制 Tab 缩进）+ `Authors`/`Entity`/`Description`/`References` 文档头 + Apache 2.0 许可证块。`-- ===` 分隔线会被 Sphinx 与 `docs/Entity.template` 解析。

文档头之后是上下文子句（`library` + `use`）。注意它 `use PoC.arith.all` 引用了自己所在的命名空间根包（因为 `arith_addw` 内部用了根包里定义的 `tArch` 等类型）：

[src/arith/arith_addw.vhdl:L46-L51](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl#L46-L51) —— `library PoC;` + `use PoC.utils.all;` + `use PoC.arith.all;`。

接着是 entity 声明。generic 用大写、带类型与默认值，端口用命名风格——这几点都要照抄：

[src/arith/arith_addw.vhdl:L54-L71](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl#L54-L71) —— `entity arith_addw is ... end entity;`，generic 包括 `N`、`K`、`ARCH : tArch := AAM` 等。

最后是可综合实现，架构名固定 `rtl`：

[src/arith/arith_addw.vhdl:L79-L81](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl#L79-L81) —— `architecture rtl of arith_addw is`，文件以 `end architecture;` 收尾。

**② 文档模板如何把源码变成文档页——`docs/Entity.template`**

PoC 的每个核在文档站点都有一页，由 `docs/Entity.template` 这个 reStructuredText 模板自动生成。它用占位符 `{EntityName}`、`{SourceRelPath}`、`{EntityDeclarationFromTo}` 等从源码里抽取信息：

[docs/Entity.template:L1-L37](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/docs/Entity.template#L1-L37) —— 模板用 `.. literalinclude::` 直接引用源码文件并按行号截取 entity 声明段。

这给了我们一条**逆向校验**：如果你的文档头不合规（缺分隔线、缺 `Entity:` 段），文档生成就会失败或错位。因此写新核时，文档头不是装饰，而是被工具消费的接口。

**③ 编码规范的硬性条款——`vhdl_coding.md`**

规范里与新核结构直接相关的三条：

[vhdl_coding.md:L8-L17](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/vhdl_coding.md#L8-L17) —— 命名与文件约定：`.vhdl` 后缀；`<namespace>_<entity>` 蛇形命名；单实体单文件 `<entity>.vhdl`；综合用 `rtl`，测试台加 `_tb` 后缀、架构名 `tb`。

[vhdl_coding.md:L19-L51](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/vhdl_coding.md#L19-L51) —— 文档头要求：编辑器配置行 + 匹配 `/^--\s*={16,}$/` 的分隔线 + `Authors|Entity|Description|SeeAlso` 段 + 许可证块。

[vhdl_coding.md:L84-L87](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/vhdl_coding.md#L84-L87) —— 例化约定：**禁止**位置绑定（generic/port 都必须命名绑定）；**优先**用 component 例化（当声明在包里可得时）——这正是「每核必补 component 声明」的规范出处。

#### 4.1.4 代码实践

**实践目标**：用目录树把「新增一个核」的最小文件清单可视化，建立空间感。

**操作步骤**：

1. 在仓库根目录列出 `src/arith/arith_addw*` 相关的全部伴生文件。
2. 对照本节「情形 A」清单，逐个标注每个文件的角色。
3. 自己画一张「文件依赖图」：`tb .files → src .files → common.files + 根包 + 核本体`。

**需要观察的现象**：你会发现 `arith_addw` 一共有 4 个同名伴生文件（`.vhdl` / `.files` / `_tb.vhdl` / `_tb.files`），加上 1 个共享的命名空间根包 `arith.pkg.vhdl`——这正是「一个核」的完整物理足迹。

**预期结果**：`.vhdl`（实现）、`.files`（编译清单）、`_tb.vhdl`（测试）、`_tb.files`（测试清单）四件套，加上被复用的根包。

> 命令示例（只读，不修改任何源码）：`ls src/arith/arith_addw* tb/arith/arith_addw*`。

#### 4.1.5 小练习与答案

**练习 1**：如果你只写了 `arith_xxx.vhdl` 而忘了写 `arith_xxx.files`，会发生什么？
**参考答案**：pyIPCMI 永远不会编译它——`.files` 是 pyIPCMI 发现源码的**唯一**入口（见 u5-l1）。文件静静躺在磁盘上，既不进 `PoC` 库，也无法被综合或仿真。

**练习 2**：为什么 `tb/<core>_tb.files` 要先 `include` 被测核的 `src/<core>.files`，而不是直接列出所有依赖文件？
**参考答案**：为了**复用**与**单一事实源**。被测核的依赖清单已经在 `src/<core>.files` 里写好（公共包 + 根包 + 本体），测试台清单只需 `include` 它一次，再加一行测试台文件即可。若改成手写全部依赖，一旦核的依赖变化就要改两处，必然漂移。

---

### 4.2 在命名空间 pkg 中声明新组件

#### 4.2.1 概念说明

承接 u3-l1：命名空间根包 `<ns>.pkg.vhdl` 是该命名空间对外的「API 目录页」，集中放三类内容——

1. **component 声明**：每个核的「插座」描述（generic + port 接口），供其他文件用 component 方式例化。
2. **type 声明**：跨核共享的枚举/记录类型，常作为 generic 的「配置旋钮」。
3. **function 声明**（实现放 `package body`）：跨核共享的辅助函数。

新核写好后，**必须**把自己的 component 声明加进根包，否则别的文件无法用 component 方式例化它（违反规范优先项）。这也是根包要「先于任何核编译」的根本原因——核内部可能 `use PoC.<ns>.all` 引用根包里的类型，外部用户也要从根包里找 component。

#### 4.2.2 核心流程

在根包里新增一个 component 声明的步骤：

```
1. 打开 src/<ns>/<ns>.pkg.vhdl
2. 在 package <ns> is ... end package; 之间，追加：
     component <ns>_<core> is
       generic ( ... );     -- 与实体 entity 声明完全一致
       port ( ... );        -- 与实体 entity 声明完全一致
     end component;
3. （若新核引入了新类型/函数）同步把 type 放 package spec、函数实现放 package body
4. 保存。component 声明无需 package body（除非还加了 function）
```

> 关键约束：component 声明里的 generic/port 必须**逐字**与 `entity` 声明一致——这是 VHDL component 例化做接口匹配的依据。两者漂移会在 elaboration 期报错。

#### 4.2.3 源码精读

**① 根包的整体结构——`arith.pkg.vhdl`**

根包同样有完整的文档头（注意 `Package:` 段而非 `Entity:` 段）：

[src/arith/arith.pkg.vhdl:L1-L32](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith.pkg.vhdl#L1-L32) —— 文档头里 `Package:` 段说明这是「`PoC.arith` 命名空间的 component 声明、类型与函数包」。

**② 一个 component 声明长什么样——以 `arith_addw` 为例**

这是本讲最该照抄的片段。它就是把 entity 的接口「拷」一份到包里，关键字从 `entity` 换成 `component`：

[src/arith/arith.pkg.vhdl:L165-L182](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith.pkg.vhdl#L165-L182) —— `component arith_addw is generic(...); port(...); end component;`。对比 [src/arith/arith_addw.vhdl:L54-L71](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl#L54-L71) 的 entity 声明，二者的 generic/port 完全一致。

**③ 共享类型放在哪——`tArch` 等**

`arith_addw` 的 generic `ARCH : tArch` 用到的枚举类型 `tArch`，就定义在同一个根包里，全命名空间共享：

[src/arith/arith.pkg.vhdl:L161-L163](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith.pkg.vhdl#L161-L163) —— `type tArch is (AAM, CAI, CCA, PAI);` 等。这正是「新核如果引入新配置旋钮，就把枚举加到根包」的范例。

**④ 何时需要 package body**

根包里如果声明了 `function`，就要配套 `package body` 放实现；纯 component/type 声明不需要 body。`arith.pkg.vhdl` 因为有 `arith_div_latency` 函数，所以有 body：

[src/arith/arith.pkg.vhdl:L238-L243](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith.pkg.vhdl#L238-L243) —— `package body arith is ... end package body;`，内含函数实现。

> 对照 u3-l1 提到的「形态因地制宜」：`fifo.pkg` 是纯组件货架（无 body），`arith.pkg` 三样俱全。新命名空间的根包按需选择即可。

#### 4.2.4 代码实践

**实践目标**：为一个虚构核 `arith_halfadd`（半加器）写出 component 声明，体会「entity 接口搬进包」的过程。

**操作步骤**：

1. 假设 `arith_halfadd` 的 entity 是：

```vhdl
-- 示例代码（非项目原有，仅供练习）
entity arith_halfadd is
  port (
    a, b : in  std_logic;
    s, c : out std_logic
  );
end entity;
```

2. 在 [src/arith/arith.pkg.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith.pkg.vhdl) 的 `package arith is` 与 `end package;` 之间，补写：

```vhdl
-- 示例代码（非项目原有，仅供练习）
component arith_halfadd is
  port (
    a, b : in  std_logic;
    s, c : out std_logic
  );
end component;
```

**需要观察的现象**：component 声明的 generic/port 段与 entity **逐字相同**，只是把 `entity ... is` 换成 `component ... is`。

**预期结果**：补完后，任何 `use PoC.arith.all;` 的文件都能用 `DUT : arith_halfadd port map(...);` 例化它。

> 待本地验证：本练习仅要求你在草稿上写出声明，**不要真的修改仓库里的 `arith.pkg.vhdl`**（本讲禁止改源码）。在心里或本地副本上演练即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 component 声明的 generic/port 必须与 entity 完全一致？
**参考答案**：VHDL 在 elaboration 阶段会把 component 例化绑定到实际 entity，按 generic/port 的**名称与类型**做匹配。两者不一致（少一个端口、类型不同、默认值不同）会导致绑定失败或 elaboration 报错。

**练习 2**：如果新核用到了一个全新的枚举类型 `tMode`，应该把它放在核自己的 `.vhdl` 里，还是根包里？
**参考答案**：放**根包**里（`package` spec 段）。因为该类型要同时被三处使用：核本体（`architecture rtl` 里）、根包里的 component 声明（generic 类型）、外部例化方。放根包里一句 `use PoC.<ns>.all` 即可全员可见；放在核文件里则外部无法引用。

---

### 4.3 配套测试台与 .files 清单

#### 4.3.1 概念说明

一个新核要被社区接受，必须配自检式测试台（承接 u4-l1、u4-l2）。PoC 测试台有两个特点：

- **自检**：用 `simAssertion` 把比对结果累计进全局 `globalSimulationStatus`，跑完一次性输出 PASSED/FAILED，不靠人眼看波形。
- **由 `.files` 驱动**：测试台文件本身也要进 `.files` 清单，且这份清单要先把被测核的依赖（经 `include`）拉全，再追加测试台文件。

`.files` 不是 VHDL，而是 pyIPCMI 的编译脚本语言（承接 u5-l1）。它用三类语句装配编译：

- `vhdl <库> "<路径>"`：把一个 VHDL 文件编译进指定库（核进 `PoC` 库，测试台进 `test` 库）。
- `include "<路径>"`：嵌入另一份 `.files` 的内容（依赖复用）。
- `if (<条件变量> = "<值>") then ... end if`：按厂商/版本/工具条件选择编译。

#### 4.3.2 核心流程

写测试台 + 清单的完整步骤：

```
1. 写 tb/<ns>/<core>_tb.vhdl：
   - 文档头（Testbench: 段）+ 许可证块
   - library/use：IEEE + PoC.<ns>.all + 仿真专用包（sim_types/simulation/waveform）
   - entity <core>_tb is end entity;          -- 空实体，无端口
   - architecture tb：
       * 常量 CLOCK_FREQ : FREQ := 100 MHz;
       * 信号 Clock / 激励 / 观察
       * simInitialize;  simGenerateClock(Clock, CLOCK_FREQ);
       * DUT 例化（命名绑定，推荐 entity 直接例化或 component 例化）
       * 激励/检查 process，用 simAssertion 累计结果，最后 simFinalize;

2. 写 src/<ns>/<core>.files（若尚不存在）：
   - include "src/common/common.files"
   - vhdl PoC "src/<ns>/<ns>.pkg.vhdl"
   - vhdl PoC "src/<ns>/<core>.vhdl"

3. 写 tb/<ns>/<core>_tb.files：
   - include "src/<ns>/<core>.files"          # 被测核依赖
   - vhdl test "tb/<ns>/<core>_tb.vhdl"        # 测试台本体
```

> 若核需要厂商原语（如 `arith_addw` 之外某些核），其 `.files` 还要在开头按 `DeviceVendor` 条件 `include lib/<Vendor>.files`，承接 u3-l2 的双层选择。

#### 4.3.3 源码精读

**① 单核编译清单——`arith_addw.files`**

这是新核 `.files` 的标准模板，四段式：编辑器注释头 → 厂商原语库（按需）→ 公共包 → 命名空间根包 + 核本体：

[src/arith/arith_addw.files:L1-L17](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.files#L1-L17) —— 注意顺序：`include "src/common/common.files"` 在前（公共包先编译），再 `vhdl PoC "src/arith/arith.pkg.vhdl"`（根包），最后 `vhdl PoC "src/arith/arith_addw.vhdl"`（核本体，注释标 `# Top-Level`）。

这条顺序就是 u3-l1 反复强调的「公共包 → 根包 → 核」编译序，错序会导致 `use PoC.arith.all` 找不到包。

**② 测试台编译清单——`sync_Bits_tb.files`（最简形态）**

`sync_Bits_tb.files` 是测试台清单的极简范本，只有两步：include 被测核清单 + 追加测试台文件：

[tb/misc/sync/sync_Bits_tb.files:L1-L11](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/misc/sync/sync_Bits_tb.files#L1-L11) —— `include "src/misc/sync/sync_Bits.files"`（被测核，注释 `# Unit Under Test`）+ `vhdl test "tb/misc/sync/sync_Bits_tb.vhdl"`（注释 `# Testbench`）。注意测试台编译进 `test` 库，而非 `PoC` 库。

**③ 自检式测试台骨架——`arith_addw_tb.vhdl`**

测试台实体是空壳（无端口）：

[tb/arith/arith_addw_tb.vhdl:L46-L47](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/arith/arith_addw_tb.vhdl#L46-L47) —— `entity arith_addw_tb is end entity;`。

架构头声明 `CLOCK_FREQ` 常量（用 `physical` 包的 `FREQ` 类型，承接 u2-l4），`begin` 后立刻两连调用仿真入口（这两个过程含 `wait`，裸写在架构体里等价隐式并发进程，承接 u4-l1）：

[tb/arith/arith_addw_tb.vhdl:L50-L75](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/arith/arith_addw_tb.vhdl#L50-L75) —— `architecture tb` + `constant CLOCK_FREQ : FREQ := 100 MHz;` + `simInitialize;` + `simGenerateClock(Clock, CLOCK_FREQ);`。

DUT 例化用三层 `for generate` 把全部 generic 组合（`tArch × tSkipping × boolean` = 多种配置）一次性展开，每组一个 DUT——这是 u4-l2 讲过的「批量验证」技巧：

[tb/arith/arith_addw_tb.vhdl:L78-L103](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/arith/arith_addw_tb.vhdl#L78-L103) —— `DUT : entity PoC.arith_addw generic map(...) port map(...);`，注意用的是**命名绑定**（符合规范）。

#### 4.3.4 代码实践

**实践目标**：为一个最简核写一份能被 pyIPCMI 编译的测试台清单（`.files`），理解 `test` 库与 `include` 的用法。

**操作步骤**：

1. 假设你已有一个虚构核 `arith_halfadd`（见 4.2.4），其 `src/arith/arith_halfadd.files` 已写好（模仿 [arith_addw.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.files)）。
2. 草拟 `tb/arith/arith_halfadd_tb.files`：

```
# 示例代码（非项目原有，仅供练习）
# Note: all files are relative to PoC root directory
include "src/arith/arith_halfadd.files"      # Unit Under Test
vhdl    test    "tb/arith/arith_halfadd_tb.vhdl"  # Testbench
```

**需要观察的现象**：测试台清单**不重复**列公共包和根包——这些已被 `include` 进来的 `arith_halfadd.files` 覆盖。

**预期结果**：pyIPCMI 读这份清单时，会先把 `PoC` 库装配好（公共包 + `arith.pkg` + `arith_halfadd`），再把测试台编译进独立的 `test` 库，最后顶层例化 `arith_halfadd_tb` 跑仿真。

> 待本地验证：实际能否跑通取决于本地是否装好 pyIPCMI 与某款仿真器（见 u5-l1）。本练习重点是清单格式正确性，可在本地用文本比对 `sync_Bits_tb.files` 验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么测试台编译进 `test` 库，而被测核编译进 `PoC` 库？
**参考答案**：分层隔离。`PoC` 库是「可发布的产品代码」，只放可综合核与公共包；`test` 库是「仅仿真用的验证代码」，含测试台与仿真专用包（`sim_*`）。这样同一个 `PoC` 库既能被综合工具消费（不含仿真专用代码），又能被仿真器配合 `test` 库消费。

**练习 2**：测试台清单里如果忘了 `include` 被测核的 `.files`，直接写 `vhdl test "tb/.../_tb.vhdl"`，会怎样？
**参考答案**：测试台文件里的 `use PoC.<ns>.all` 和 DUT 例化会失败——因为 `PoC` 库里根本没有被测核（也没编译它依赖的根包/公共包）。`include` 的作用就是把「让被测核可用」所需的全部编译步骤一并带入。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个贯穿任务：**新建一个命名空间 `mylib`，并在其下实现一个最简核 `mylib_echo`（时钟寄存器型回显：输入打一拍送到输出），配上根包、component 声明、单核清单、自检测试台与测试台清单**。

> 以下全部为**示例代码（非项目原有，仅供练习）**。请在本地副本或草稿上完成，不要写入本仓库源码树（本讲禁止改源码；且 `mylib` 仅为教学虚构命名空间）。

### 5.1 规划文件清单（情形 B：新建命名空间）

```
src/mylib/mylib.pkg.vhdl         # 命名空间根包（component 声明）
src/mylib/mylib_echo.vhdl        # 核本体（entity + architecture rtl）
src/mylib/mylib_echo.files       # 单核编译清单
tb/mylib/mylib_echo_tb.vhdl      # 测试台
tb/mylib/mylib_echo_tb.files     # 测试台编译清单
```

### 5.2 写根包 `mylib.pkg.vhdl`

```vhdl
-- 示例代码（非项目原有，仅供练习）
-- EMACS settings: -*-  tab-width: 2; indent-tabs-mode: t -*-
-- =============================================================================
-- Authors:              <Your Name>
-- Package:              VHDL package for the PoC.mylib namespace
-- License:
-- =============================================================================
-- (此处省略完整 Apache 2.0 许可证块，实际贡献时必须完整填写)

library IEEE;
use     IEEE.std_logic_1164.all;

package mylib is
  component mylib_echo is
    generic (
      N : positive                      -- 数据位宽
    );
    port (
      Clock  : in  std_logic;
      Input  : in  std_logic_vector(N-1 downto 0);
      Output : out std_logic_vector(N-1 downto 0)
    );
  end component;
end package;
```

说明：`mylib_echo` 没有引入新类型/函数，故**无需 `package body`**（对照 u3-l1 的 `fifo.pkg` 纯货架形态）。

### 5.3 写核本体 `mylib_echo.vhdl`

```vhdl
-- 示例代码（非项目原有，仅供练习）
-- EMACS settings: -*-  tab-width: 2; indent-tabs-mode: t -*-
-- =============================================================================
-- Authors:              <Your Name>
-- Entity:               mylib_echo
-- Description:
--   一个最简的时钟寄存器型回显核：把 Input 打一拍送到 Output。
-- License:
-- =============================================================================
-- (此处省略完整许可证块)

library IEEE;
use     IEEE.std_logic_1164.all;

entity mylib_echo is
  generic (
    N : positive                      -- 数据位宽
  );
  port (
    Clock  : in  std_logic;
    Input  : in  std_logic_vector(N-1 downto 0);
    Output : out std_logic_vector(N-1 downto 0)
  );
end entity;


architecture rtl of mylib_echo is
  signal q : std_logic_vector(N-1 downto 0) := (others => '0');  -- 时序信号必须给初值
begin
  process(Clock) is
  begin
    if rising_edge(Clock) then
      q <= Input;
    end if;
  end process;
  Output <= q;
end architecture;
```

> 自检要点：① 文件名 = `mylib_echo.vhdl` = `<namespace>_<entity>`；② 架构名 `rtl`；③ 时序信号 `q` 给了初值（遵守 u1-l4 信号初始化规范）；④ 本例无需厂商专用子实体——它只用通用触发器，综合器能直接推断，因此**不必**走 u3-l2 的双层选择（这是判断「是否需要可移植包装」的决策点）。

### 5.4 写单核清单 `mylib_echo.files`

```
# 示例代码（非项目原有，仅供练习）
# EMACS settings: -*- tab-width: 2; indent-tabs-mode: t -*-
# Note: all files are relative to PoC root directory
include      "src/common/common.files"            # 公共包
vhdl   PoC   "src/mylib/mylib.pkg.vhdl"           # PoC.mylib 根包
vhdl   PoC   "src/mylib/mylib_echo.vhdl"          # Top-Level
```

> `mylib_echo` 不依赖任何厂商原语，故**不需要**开头的 `if (DeviceVendor = ...) then include lib/...` 段。若将来它要用 Xilinx 进位链，再按 [arith_addw.files:L8-L10](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.files#L8-L10) 的写法补上。

### 5.5 写测试台 `mylib_echo_tb.vhdl`

```vhdl
-- 示例代码（非项目原有，仅供练习）
-- EMACS settings: -*-  tab-width: 2; indent-tabs-mode: t -*-
-- =============================================================================
-- Authors:              <Your Name>
-- Testbench:            mylib_echo_tb
-- License:
-- =============================================================================
-- (此处省略完整许可证块)

library IEEE;
use     IEEE.std_logic_1164.all;

library PoC;
use     PoC.physical.all;        -- 为了 FREQ
use     PoC.mylib.all;           -- 拉入 mylib_echo 的 component 声明
-- simulation only packages
use     PoC.sim_types.all;
use     PoC.simulation.all;
use     PoC.waveform.all;


entity mylib_echo_tb is
end entity;


architecture tb of mylib_echo_tb is
  constant CLOCK_FREQ : FREQ := 100 MHz;
  constant N : positive := 4;

  signal Clock  : std_logic;
  signal Input  : std_logic_vector(N-1 downto 0) := (others => '0');
  signal Output : std_logic_vector(N-1 downto 0);
begin
  simInitialize;
  simGenerateClock(Clock, CLOCK_FREQ);

  -- DUT：命名绑定
  DUT : entity PoC.mylib_echo
    generic map ( N => N )
    port map (
      Clock  => Clock,
      Input  => Input,
      Output => Output
    );

  -- 激励 + 自检：回显核输出应等于上一拍的输入
  procStim : process is
    constant simProcessID : T_SIM_PROCESS_ID := simRegisterProcess("echo checker");
  begin
    for i in 0 to 15 loop
      Input <= std_logic_vector(to_unsigned(i, N));
      wait until rising_edge(Clock);
      -- Output 是 Input 打一拍，此处给出检查思路（精确比较需多等一拍）
      wait until rising_edge(Clock);
      simAssertion(Output = std_logic_vector(to_unsigned(i, N)),
                   "echo mismatch at i=" & integer'image(i));
    end loop;
    simDeactivateProcess(simProcessID);
    simFinalize;
    wait;
  end process;
end architecture;
```

> 自检要点：① 实体空壳 `mylib_echo_tb is end entity;`、架构名 `tb`；② `simInitialize` + `simGenerateClock` 两连调用；③ DUT 用命名绑定；④ 用 `simAssertion` 累计结果、`simFinalize` 收尾——对照 [arith_addw_tb.vhdl:L106-L148](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/arith/arith_addw_tb.vhdl#L106-L148) 的检查 process 写法。（`to_unsigned` 需 `use IEEE.numeric_std.all;`，实际贡献时请补上。）

### 5.6 写测试台清单 `mylib_echo_tb.files`

```
# 示例代码（非项目原有，仅供练习）
include   "src/mylib/mylib_echo.files"        # Unit Under Test
vhdl test "tb/mylib/mylib_echo_tb.vhdl"        # Testbench
```

### 5.7 完成后自测

逐一核对：

- [ ] 5 个文件全部就位，文件名遵守 `<namespace>_<entity>` 蛇形命名。
- [ ] 每个 `.vhdl` 有编辑器配置行 + 文档头（`Authors`/`Entity` 或 `Package` 或 `Testbench` 段）+ 完整许可证块。
- [ ] 根包 `mylib.pkg.vhdl` 的 component 声明与 `mylib_echo` entity 的 generic/port 逐字一致。
- [ ] `mylib_echo.files` 编译顺序为「公共包 → 根包 → 本体」。
- [ ] 测试台编译进 `test` 库，且清单先 `include` 被测核清单。

**预期结果**：在装好 pyIPCMI 与仿真器的本地环境，把 `tb/mylib/mylib_echo_tb.files` 喂给 pyIPCMI，应能完成编译并跑出 `PASSED`（待本地验证）。

---

## 6. 本讲小结

- 新增一个核的最小文件清单是**四件套**：`<core>.vhdl`（实现）、`<core>.files`（编译清单）、`<core>_tb.vhdl`（测试台）、`<core>_tb.files`（测试清单），外加被复用的命名空间根包 `<ns>.pkg.vhdl`。
- 新建一个命名空间 = 上面四件套 + 一份根包 `mylib.pkg.vhdl`（情形 B 是情形 A 的超集）。
- **每核必补 component 声明**到根包——这是 PoC 规范「优先 component 例化」的硬性要求，声明须与 entity 接口逐字一致。
- `.files` 是 pyIPCMI 的编译脚本（非 VHDL），用 `vhdl`/`include`/`if` 三类语句装配；编译顺序固定为「公共包 → 根包 → 核本体」，测试台另起 `test` 库。
- 自检式测试台 = 空实体 + `architecture tb` + `simInitialize`/`simGenerateClock` 两连调用 + 命名绑定 DUT + `simAssertion`/`simFinalize` 收尾。
- 决策点：核若只用通用 RTL（如 `mylib_echo`）则一份 `rtl` 即可；若要用厂商原语（进位链、BRAM 原语等）才需走 u3-l2 的「包装实体 + 厂商子实体 + `.files` 条件 include」双层可移植路线。

---

## 7. 下一步学习建议

- **跑通一次真实编译**：回到 u5-l1，在你本地装好 pyIPCMI，挑一个已有核（如 `arith_addw`）的 `tb/arith/arith_addw_tb.files`，用 `poc.sh`/`poc.ps1` 跑一次仿真，亲眼看到 `.files` 是如何被消费的。
- **读一个「带厂商选择」的核**：对照本讲的「决策点」，去读 [src/misc/sync/sync_Bits.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl) 与其 `.files`，体会「需要可移植时」额外要写的包装实体与条件 include。
- **贡献回上游**：本仓库 `VLSI-EDA/PoC` 是历史快照（见 u1-l1），真正在维护的是 `VHDL/PoC`（用 GHDL/NVC + OSVVM 经 GitHub Actions 检查）。若要正式贡献，请转到 `VHDL/PoC`，并按其 CI 要求补齐 OSVVM 风格测试。
- **复习闭环**：若对其中任何一环仍有模糊，回看 u1-l4（规范）、u3-l1（根包模式）、u4-l2（测试台写法）、u5-l1（pyIPCMI 与 `.files`）。
