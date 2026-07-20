# 编码约定、错误消息机制与二次开发指南

## 1. 本讲目标

本讲是 psi_tb 学习手册的收官篇，不再引入新过程，而是退后一步看「整座库是用什么规矩搭起来的」。

学完后你应当能够：

1. 说清 `###ERROR###` 前缀为何是全库与 CI 之间的硬性契约，以及 `severity error` 不中断仿真背后的设计意图。
2. 画出 `txt_util → compare → activity → i2c` 的分层复用链，并能用 `config.tcl` 的编译顺序佐证。
3. 看懂 I2C 包里 `GenMessage` / `GenMessageNoPrefix` + `MsgInfo_r` 这套结构化消息生成机制，并解释为何复用 `CheckLastActivity` 时要剥掉前缀。
4. 仿照 `psi_tb_compare_pkg` 的既有风格，亲手新增一个 `StdlvCompareReal` 检查过程，并把它接入一个能被 CI 判定的 testbench。

本讲把前面 7 个单元里反复出现但从未集中说明的「潜规则」一次性讲透，并落到一个可编译、可验证的二次开发实战上。

## 2. 前置知识

在进入正文前，确认你已经理解下面几个概念（它们都来自前面的讲义，本讲直接使用，不再展开）：

- **testbench 不可综合**：psi_tb 的代码只服务仿真，因此可以自由使用 `assert` / `report` / `textio` / `wait for` 等语法。详见 u1-l1。
- **`###ERROR###` 与 CI 的关系**：`run.tcl` 末尾的 `run_check_errors "###ERROR###"` 会扫描 Transcript；`ciFlow.py` 再独立检查 Transcript。详见 u1-l3、u8-l1。
- **`assert ... report ... severity error` 骨架**：比较与活动检查过程都沿用同一套断言骨架，`severity error` 只打印、不停止仿真。详见 u3-l1。
- **复用关系**：`activity_pkg` 内部调用 `compare_pkg` 的过程，`i2c_pkg` 又调用 `activity_pkg` 的 `CheckLastActivity`。详见 u4-l1、u7-l3。
- **`to_string` 重载**：`txt_util` 提供了 integer / real / signed / unsigned / std_logic_vector 多个 `to_string` 重载，其中 real 重载内部就是 `real'image`。详见 u2-l1。

如果你对其中某项还不熟，建议先回到对应讲义读一遍相关章节再继续。

## 3. 本讲源码地图

本讲横跨四个文件，职责如下：

| 文件 | 在本讲的作用 |
| --- | --- |
| [README.md](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md) | 给出「一包一文件」「只收 testbench 代码」等成文约定 |
| [hdl/psi_tb_compare_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd) | 统一前缀、统一断言骨架的样板间，也是二次开发的模仿对象 |
| [hdl/psi_tb_activity_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd) | 复用链的中间层：它 `use` 了 compare 包，并在过程体里直接调用 `StdlCompare` / `StdlvCompareInt` |
| [hdl/psi_tb_i2c_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd) | 复用链的顶层：定义 `MsgInfo_r` 与 `GenMessage` / `GenMessageNoPrefix`，并复用 `CheckLastActivity` |

另外会引用两个脚本文件来佐证 CI 契约与编译顺序：

- [sim/run.tcl](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl) —— `run_check_errors "###ERROR###"` 的调用点。
- [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl) —— `add_sources -tag lib/src/tb` 的编译顺序。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块。前三个是对既有约定的归纳，第四个是「按这些约定新增一个过程」的实战。

### 4.1 统一 Prefix 参数与 ###ERROR### 约定

#### 4.1.1 概念说明

psi_tb 里所有「会判定对错并打印诊断消息」的过程，参数表末尾都挂着一个同名参数：

```vhdl
Prefix : in string := "###ERROR###: "
```

它不是装饰，而是全库与 CI 之间的**契约字面量**。过程在断言失败时这样拼消息：

```vhdl
report Prefix & Msg & " [Expected ... , Received ...]"
severity error;
```

于是每一条自检失败的消息都以 `###ERROR###: ` 开头。而 `run.tcl` 末尾正好扫这个子串：

```tcl
run_check_errors "###ERROR###"
```

二者通过**同一个字符串**咬合：testbench 一旦自检失败，就自动变成 CI 失败，无需任何额外接线。

> 关键认知：判定 CI 失败靠的是「`###ERROR###` 这个子串是否出现」，**不是** `severity` 级别。所以全库统一用 `severity error`（只打印、不中断），目的恰恰是让一次仿真把所有不匹配都暴露出来，而不是在第一个错误处就停下来。

#### 4.1.2 核心流程

一次自检失败如何变成 CI 红灯，流程如下：

1. 仿真运行到某个比较/活动检查过程。
2. `assert` 条件为 `false`，触发 `report Prefix & Msg & ...`，把 `###ERROR###: ...` 写进 Transcript。
3. `severity error` **不停止仿真**，仿真继续跑到结束。
4. `run.tcl` 末尾 `run_check_errors "###ERROR###"` 扫描 Transcript，命中即报错。
5. `ciFlow.py` 再独立读 `Transcript.transcript`，做第二重判定（详见 u8-l1）。

容差判定用统一的「容差带」思想，即实际值落在期望值的对称区间内即算通过：

\[ \text{Actual} \in [\,\text{Expected}-T,\ \text{Expected}+T\,] \]

对应代码就是：

```vhdl
assert (Actual >= Expected - Tolerance) and (Actual <= Expected + Tolerance)
```

#### 4.1.3 源码精读

先看契约在比较包里是如何被**逐字复制**的。`StdlvCompareInt` 是最具代表性的样本，它的参数表（含 `Prefix` 默认值）和断言骨架如下：

参数表默认值（[hdl/psi_tb_compare_pkg.vhd:L26-L31](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L26-L31)）——注意 `Prefix : in string := "###ERROR###: "`：

```vhdl
procedure StdlvCompareInt(Expected  : in integer;
                          Actual    : in std_logic_vector;
                          Msg       : in string;
                          IsSigned  : in boolean := true;
                          Tolerance : in integer := 0;
                          Prefix    : in string  := "###ERROR###: ");
```

断言骨架（[hdl/psi_tb_compare_pkg.vhd:L134-L139](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L134-L139)）——注意 `Prefix & Msg & "[Expected ... Received ...]"` 的固定拼法与 `severity error`：

```vhdl
assert (ActualInt_v >= Expected - Tolerance) and (ActualInt_v <= Expected + Tolerance)
report Prefix & Msg &
            " [Expected " & integer'image(Expected) & "(0x" & hstr(ExpectedStdlv32_v) & ")" &
            ", Received " & integer'image(ActualInt_v) & "(0x" & hstr(ActualStdlv32_v) & ")" &
            ", Tolerance " & integer'image(Tolerance) & "]"
severity error;
```

这套「`Prefix & Msg & "[Expected ..., Received ..., Tolerance ...]"` + `severity error`」的骨架，在 `IntCompare`、`RealCompare`、`StdlCompare`、`StdlvCompareStdlv`、`SignCompare`、`UsignCompare` 里**几乎逐字相同**，只是中间的字段渲染方式（十进制 / 十六进制 / 二进制）随类型变化。例如 `IntCompare` 的骨架（[hdl/psi_tb_compare_pkg.vhd:L203-L208](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L203-L208)）与上面只有 `to_string` / `integer'image` 的差别。

再看契约的「另一端」——CI 怎么接住它。[sim/run.tcl:L32](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl#L32) 的扫描字符串与上面的 `Prefix` 默认值**完全一致**：

```tcl
run_check_errors "###ERROR###"
```

`Prefix` 既是「给人看的可读前缀」，又是「给机器看的失败标记」，一举两得。这也是为什么全库都保留这个默认值而不去改它——改了就会和 `run.tcl` 脱钩。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是亲眼确认「契约字面量在全库一致」。

1. 实践目标：统计全库有多少个过程默认 `Prefix := "###ERROR###: "`，并确认它们与 `run.tcl` 的扫描串一致。
2. 操作步骤：在仓库根目录用 Grep 搜索 `###ERROR###`，分别看 `hdl/` 与 `sim/` 两类命中。
3. 需要观察的现象：`hdl/` 下应只在 `psi_tb_compare_pkg.vhd`、`psi_tb_activity_pkg.vhd`、`psi_tb_i2c_pkg.vhd` 三个文件命中，且都是 `Prefix : in string := "###ERROR###: "` 形式；`sim/` 下应只在 `run.tcl`、`runGhdl.tcl` 各命中一处 `run_check_errors "###ERROR###"`。
4. 预期结果：前后两端的字符串逐字符相同，证明契约成立。如本地无 Grep 工具，可手动对照本讲引用的源码行号确认。
5. 无法在本讲确定运行结果时，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么比较过程都用 `severity error` 而不用 `severity failure`？

**参考答案**：`error` 只打印消息、不中断仿真，能让一次跑完收集**所有**不匹配；`failure` 会在第一个错误处立即停止，后续用例无法继续。CI 靠 `###ERROR###` 子串判定失败，不依赖 severity 级别，所以 `error` 是更合适的选择。

**练习 2**：如果有人把某个过程的 `Prefix` 默认值改成了空字符串 `""`，CI 还能正确判定它的失败吗？

**参考答案**：不能。`run_check_errors "###ERROR###"` 找不到该子串，该过程的自检失败会被 CI 漏判为「通过」。这正是全库必须保留同一默认值的原因。

---

### 4.2 compare→activity→bfm 的分层复用链

#### 4.2.1 概念说明

psi_tb 不是一堆平铺的过程，而是一条**自下而上的复用链**：

```
txt_util（字符串/数值互转）
        ↑ use
compare（比较与容差判定）
        ↑ use
activity（信号活动检查、时钟同步等待）
        ↑ use
i2c / axi / textfile（各总线 BFM 与位真驱动）
```

每一层只依赖它下面的层，且**通过普通的过程调用**把低层能力组合成高层语义。这样做有三个好处：

1. 低层逻辑（如容差判定、消息拼接）只写一遍，全库共享，行为一致。
2. 新增一个 BFM 时，直接复用成熟的检查与等待原语，不必重新造轮子。
3. 编译顺序天然确定——被 `use` 的包必须先编译，这与 VHDL 库依赖规则一致。

#### 4.2.2 核心流程

复用链在两个层面体现：

**层间依赖（编译期）**：每个包顶部的 `library work; use work.xxx.all;` 决定了它依赖谁。编译必须按「被依赖者先」的拓扑顺序进行。

**层内调用（运行期）**：高层过程在过程体里直接 `call` 低层过程，把低层的断言能力「嵌」进自己的语义。例如 `CheckNoActivityStlv` 内部调用 `StdlvCompareInt` 做电平校验——活动段（`Sig'last_event`）自己写，电平段复用 compare 包。

`config.tcl` 的 `add_sources -tag lib/src/tb` 三段式，恰好把这条复用链物化成编译顺序：

```
lib : psi_common 包
src : psi_tb_txt_util → psi_tb_compare_pkg → psi_tb_activity_pkg → psi_tb_i2c_pkg
tb  : testbench
```

`-tag` 的出现顺序就是编译顺序（详见 u1-l2、u8-l1）。

#### 4.2.3 源码精读

**层间依赖的代码证据。** `activity_pkg` 的 use 子句直接 `use` 了 compare 包与 txt_util（[hdl/psi_tb_activity_pkg.vhd:L15-L17](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L15-L17)）：

```vhdl
library work;
use work.psi_tb_txt_util.all;
use work.psi_tb_compare_pkg.all;
```

`i2c_pkg` 则站在更顶层，把 compare、activity、txt_util 以及两个 psi_common 包全部 `use`（[hdl/psi_tb_i2c_pkg.vhd:L14-L19](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L14-L19)）：

```vhdl
library work;
    use work.psi_tb_compare_pkg.all;
    use work.psi_tb_activity_pkg.all;
    use work.psi_tb_txt_util.all;
    use work.psi_common_logic_pkg.all;
    use work.psi_common_math_pkg.all;
```

**层内调用的代码证据。** `CheckNoActivityStlv` 的活动段自己用 `Sig'last_event` 写断言，电平段则**直接调用** `StdlvCompareInt`（[hdl/psi_tb_activity_pkg.vhd:L119-L125](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L119-L125)）：

```vhdl
wait for IdleTime;
assert Sig'last_event >= IdleTime
report Prefix & Msg & "[Unexpected Activity]"
severity error;
if Level /= -1 then
    StdlvCompareInt(Level, Sig, "CheckNoActivityStlv: " & Msg, false, 0, Prefix);
end if;
```

注意它把 `Prefix` **原样透传**给 `StdlvCompareInt`——这是复用链里保持消息风格统一的关键习惯：上层把前缀传下去，低层断言失败时打印的前缀就和上层一致。

同一个 `StdlvCompareInt` 还被 `SignCompareInt` / `UsignCompareInt` 当作实现细节复用——它们只是先 `std_logic_vector(Actual)` 转一下类型，再把调用整体委托出去（[hdl/psi_tb_compare_pkg.vhd:L278-L298](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L278-L298)）：

```vhdl
procedure SignCompareInt(...) is
begin
    StdlvCompareInt(Expected  => Expected,
                    Actual    => std_logic_vector(Actual),
                    Msg       => Msg,
                    IsSigned  => true,
                    Tolerance => Tolerance,
                    Prefix    => Prefix);
end procedure;
```

这就是「薄包装」模式：高层过程只做类型适配与参数固定，核心断言与消息由低层统一负责。

**编译顺序的代码证据。** `config.tcl` 把 src 段的四个包按依赖顺序列出（[sim/config.tcl:L28-L33](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L28-L33)）：

```tcl
add_sources "../hdl" {
    psi_tb_txt_util.vhd \
    psi_tb_compare_pkg.vhd \
    psi_tb_activity_pkg.vhd \
    psi_tb_i2c_pkg.vhd \
} -tag src
```

`txt_util` 必须排在最前，因为它被所有人 `use`；`i2c_pkg` 排最后，因为它 `use` 了前面三个。顺序错了会直接报「找不到被引用的库单元」的编译错误。

#### 4.2.4 代码实践

这是一个**调用链跟踪型实践**。

1. 实践目标：从一条 I2C 主机调用出发，跟踪它如何一路复用到 compare 包。
2. 操作步骤：
   - 阅读 [hdl/psi_tb_i2c_pkg.vhd:L238-L264](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L238-L264) 的 `SendBitInclClock`，找到它对 `CheckLastActivity` 的调用（约 259、261 行）。
   - 再跳到 [hdl/psi_tb_activity_pkg.vhd:L129-L143](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L129-L143) 的 `CheckLastActivity`，找到它对 `StdlCompare` 的调用（约 141 行）。
   - 最后到 [hdl/psi_tb_compare_pkg.vhd:L159-L175](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L159-L175) 的 `StdlCompare`，看到真正的 `assert ... severity error`。
3. 需要观察的现象：`Prefix` 是如何从 I2C 层的 `MsgInfo.Prefix` 一路透传到最底层 `StdlCompare` 的 `report Prefix & ...`。
4. 预期结果：画出 `I2cMasterSendByte → SendByteInclClock → SendBitInclClock → CheckLastActivity → StdlCompare` 这条四层调用链，并标注 `Prefix` 在每一层的传递点。
5. 如某段调用关系无法在本地打开源码确认，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `config.tcl` 里 `psi_tb_txt_util.vhd` 必须排在 `psi_tb_compare_pkg.vhd` 之前？

**参考答案**：`compare_pkg` 顶部有 `use work.psi_tb_txt_util.all`，VHDL 要求被引用的库单元先编译。`-tag src` 的列出顺序就是编译顺序，所以 `txt_util` 必须在前。

**练习 2**：`CheckNoActivityStlv` 复用 `StdlvCompareInt` 时，第 4 个参数传了 `false`，这代表什么？

**参考答案**：那是 `IsSigned` 参数。传 `false` 表示把 `std_logic_vector` 当**无符号**整数与 `Level` 比较，符合「总线电平值是非负整数」的语义。

---

### 4.3 i2c GenMessage / GenMessageNoPrefix 结构化消息生成

#### 4.3.1 概念说明

比较包的消息是「`Prefix & Msg & "[Expected..., Received...]"`」这种单层字符串。但 I2C 包的错误场景更复杂：同一个「SDA 在 SCL 高电平期间不稳定」可能发生在地址字节、数据字节、ACK 位的任何一个 bit 上。为了让消息能一眼定位「哪个过程、什么错误、哪个 bit、用户给的是什么描述」，I2C 包引入了一套**结构化消息生成机制**：

- 一个局部 record `MsgInfo_r`，打包 `Prefix` / `Func` / `User` 三段信息。
- 两个工厂函数 `GenMessage` / `GenMessageNoPrefix`，把 record 与一条 `General` 描述拼成固定格式的字符串。

固定格式长这样：

```
<Prefix>- <Func> - <General> - <User>
```

其中 `Func` 是过程名（如 `I2cMasterSendStart`），`General` 是具体的错误描述（如 `SCL must be 1 before procedure is called`），`User` 是用户在调用时传入的用例描述（如 `M: 7b start`）。

#### 4.3.2 核心流程

消息生成的流程分三步：

1. **打包**：每个公开的 I2C 过程一进来，就把自己的 `(Prefix, 过程名, 用户 Msg)` 打包成一个 `MsgInfo_r` 常量。
2. **分发**：过程内部调用私有原语（如 `LevelCheck`、`SendBitInclClock`）时，把整个 `MsgInfo` 作为参数传下去，而不是散着传三个字符串。
3. **渲染**：私有原语在断言失败时调用 `GenMessage(Msg.Prefix, Msg.Func, GeneralMsg, Msg.User)` 拼出最终字符串。

这里有一个**关键区分**：

- 私有原语自己直接 `report` 时用 `GenMessage`（带前缀，拼出完整的 `###ERROR###: - ...`）。
- 但当私有原语要**复用 `CheckLastActivity`** 时，必须改用 `GenMessageNoPrefix`（不带前缀）作为 `Msg` 传入——因为 `CheckLastActivity` 自己会再 `Prefix & Msg` 拼一次，若 `Msg` 里已经含前缀，就会出现两遍 `###ERROR###`。

#### 4.3.3 源码精读

先看 record 与两个工厂函数的定义。`MsgInfo_r` 打包三段信息（[hdl/psi_tb_i2c_pkg.vhd:L153-L157](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L153-L157)）：

```vhdl
type MsgInfo_r is record
    Prefix  : string;
    Func    : string;
    User    : string;
end record;
```

两个工厂函数的差别**仅在是否拼接 `Prefix`**（[hdl/psi_tb_i2c_pkg.vhd:L168-L183](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L168-L183)）：

```vhdl
function GenMessage(Prefix, Func, General, User : in string) return string is
begin
    return Prefix & "- " & Func & " - " & General & " - " & User;
end function;

function GenMessageNoPrefix(Func, General, User : in string) return string is
begin
    return Func & " - " & General & " - " & User;
end function;
```

再看「直接 report 用 `GenMessage`」的样板——`LevelCheck` 在断言里调用 `GenMessage`（[hdl/psi_tb_i2c_pkg.vhd:L186-L197](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L186-L197)）：

```vhdl
procedure LevelCheck(Expected : in std_logic; signal Sig : in std_logic;
                     Msg : in MsgInfo_r; GeneralMsg : in string) is
begin
    if (Expected = '0') or (Expected = '1') then
        assert ((Expected = '0') and (Sig = '0')) or ((Expected = '1') and ((Sig = '1') or (Sig = 'H')))
            report GenMessage(Msg.Prefix, Msg.Func, GeneralMsg, Msg.User)
            severity error;
    end if;
end procedure;
```

注意 `LevelCheck` 还顺带示范了「判高必须同时认 `'1'` 与 `'H'`」这条上拉总线约定（详见 u7-l1）。

接着看「复用 `CheckLastActivity` 时改用 `GenMessageNoPrefix`」的样板——`SendBitInclClock` 在 SCL 高电平段调用 `CheckLastActivity`，把 `General` 描述塞进 `GenMessageNoPrefix` 当 `Msg` 传过去（[hdl/psi_tb_i2c_pkg.vhd:L259-L261](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L259-L261)）：

```vhdl
CheckLastActivity(Scl, ClkHalfPeriod*0.9, -1,
    GenMessageNoPrefix(Msg.Func, "SCL high period too short [" & BitInfo & "]", Msg.User), Msg.Prefix);
...
CheckLastActivity(Sda, ClkHalfPeriod, -1,
    GenMessageNoPrefix(Msg.Func, "SDA not stable during SCL pulse [" & BitInfo & "]", Msg.User), Msg.Prefix);
```

这里 `Msg.Prefix` 通过 `CheckLastActivity` 的 `Prefix` 形参单独传入，`GenMessageNoPrefix` 只负责 `Func/General/User` 三段——分工明确，绝不重复拼前缀。

最后看「公开过程如何打包 `MsgInfo`」——每个 I2C 主机/从机过程第一行就构造这个常量（[hdl/psi_tb_i2c_pkg.vhd:L418-L426](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L418-L426)）：

```vhdl
procedure I2cMasterSendStart(...) is
    constant MsgInfo : MsgInfo_r := (Prefix, "I2cMasterSendStart", Msg);
begin
    LevelCheck('1', Scl, MsgInfo, "SCL must be 1 before procedure is called");
    LevelCheck('1', Sda, MsgInfo, "SDA must be 1 before procedure is called");
    ...
```

`Func` 字段是**写死的过程名字符串**，`User` 就是用户传入的 `Msg`。这一行之后，过程体内所有私有原语都拿同一个 `MsgInfo` 做错误定位。

#### 4.3.4 代码实践

这是一个**消息格式观察型实践**。

1. 实践目标：亲眼看到 `GenMessage` 拼出的完整消息格式。
2. 操作步骤：
   - 打开 [testbench/psi_tb_i2c_pkg_tb.vhd:L48-L50](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L48-L50)，看到 master 侧第一个用例正常调用 `I2cMasterSendStart` / `I2cMasterSendAddr`，而 slave 侧 [testbench/psi_tb_i2c_pkg_tb.vhd:L179-L181](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L179-L181) 用 `I2cSlaveWaitStart` / `I2cSlaveExpectAddr` 配对。
   - 在脑中（或纸面）模拟「故意把 slave 的 `I2cSlaveExpectAddr` 地址改成与 master 不一致」，触发 `LevelCheck` 或 `CheckBitInclClock` 失败。
3. 需要观察的现象：失败消息应是 `###ERROR###: - I2cMasterSendAddr 7b - <General> - M: 7b address` 这样的四段式。
4. 预期结果：能指出消息中 `Prefix`、`Func`、`General`、`User` 四段分别来自代码的哪一处。实际跑仿真需本地 PsiSim 环境，本讲标注「待本地验证」。
5. 想真正触发失败消息，可在本地把 slave 侧某个期望地址改错后跑 `sim/run.tcl`，观察 Transcript。

#### 4.3.5 小练习与答案

**练习 1**：为什么传给 `CheckLastActivity` 的 `Msg` 要用 `GenMessageNoPrefix`，而不是 `GenMessage`？

**参考答案**：`CheckLastActivity` 内部自己会 `report Prefix & Msg & ...`。如果 `Msg` 里已经含 `Prefix`（用 `GenMessage` 拼过），就会在消息里出现两遍 `###ERROR###`。用 `GenMessageNoPrefix` 只给 `Func/General/User` 三段，让前缀由 `CheckLastActivity` 自己拼一次。

**练习 2**：`MsgInfo_r` 的 `Func` 字段为什么要在每个公开过程里写死成过程名？

**参考答案**：为了让错误消息能一眼定位「是哪个过程报的错」。因为私有原语（如 `LevelCheck`）会被几十个公开过程共用，如果不把当前过程名随 `MsgInfo` 带下来，出错时根本无法分辨是 `I2cMasterSendStart` 还是 `I2cMasterSendStop` 报的。

---

### 4.4 二次开发约定：如何新增一个检查过程

#### 4.4.1 概念说明

psi_tb 在 [README.md:L16-L25](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md#L16-L25) 明确写了收哪些代码：BFM、值检查函数、自动激励生成，并且 [README.md:L20](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md#L20) 写明「one .vhd file per Package」。所以当你需要一个库还没有的检查过程时，正确的做法是**把它加进对应的现有 package**（而不是新建一个零散文件），并严格沿用既有约定。

一个「合格」的新增检查过程应当满足下面这张清单：

| 约定 | 具体要求 |
| --- | --- |
| 文件归属 | 加进功能最贴近的现有 package（比较类加进 `psi_tb_compare_pkg`） |
| 参数末尾 | 必须有 `Prefix : in string := "###ERROR###: "` |
| 容差参数 | 数值类比较必须有 `Tolerance`，默认 `0` 或 `0.0` |
| 断言骨架 | `assert (通过条件) report Prefix & Msg & "[Expected ..., Received ..., Tolerance ...]" severity error;` |
| 字符串渲染 | 用 `txt_util` 的 `to_string` / `hstr` / `str`，保持风格统一 |
| 复用优先 | 能复用低层过程就复用，不要重写容差判定 |

本模块的实战目标是新增 `StdlvCompareReal`：把一个 `std_logic_vector` 按「无符号定点」解释成 `real`，再与期望实数做容差比较——这是 `psi_tb_compare_pkg` 现有过程都做不到的（现有过程要么比整数，要么比位串，没有「定点转实数」这一档）。

#### 4.4.2 核心流程

`StdlvCompareReal` 的判定流程：

1. 把 `Actual`（N 位 `std_logic_vector`，含 F 位小数）拆成整数部分与小数部分。
2. 整数部分 \( I = \text{to\_integer}(\text{unsigned}(\text{Actual}(N-1 \dots F))) \)。
3. 小数部分对应的实数值 \( f = \dfrac{\text{to\_integer}(\text{unsigned}(\text{Actual}(F-1 \dots 0)))}{2^{F}} \)。
4. 实际实数值 \( v = I + f \)。
5. 套统一容差带判定：

\[ v \in [\,\text{Expected}-T,\ \text{Expected}+T\,] \]

6. 失败时按统一骨架拼 `Prefix & Msg & "[Expected ..., Received ..., Tolerance ...]"` 并 `severity error`。

定点解释的含义：一个 N 位、F 位小数的无符号定点数，其能表示的最小步长是 \( 2^{-F} \)，数值范围是 \( [0,\ 2^{N-F}-2^{-F}] \)。例如 8 位、4 位小数时，`10011001` 表示 \( 9 + 9/16 = 9.5625 \)。

#### 4.4.3 源码精读

模仿对象是 `RealCompare`——它已经是「real + 容差」的标准模板。其参数表（[hdl/psi_tb_compare_pkg.vhd:L58-L62](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L58-L62)）：

```vhdl
procedure RealCompare(Expected  : in real;
                      Actual    : in real;
                      Msg       : in string;
                      Tolerance : in real   := 0.0;
                      Prefix    : in string := "###ERROR###: ");
```

其断言骨架（[hdl/psi_tb_compare_pkg.vhd:L212-L224](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L212-L224)）：

```vhdl
assert (Actual >= Expected - Tolerance) and (Actual <= Expected + Tolerance)
report Prefix & Msg &
            " [Expected " & to_string(Expected) &
            ", Received " & to_string(Actual) &
            ", Tolerance " & to_string(Tolerance) & "]"
severity error;
```

把这两段当作模板，新增 `StdlvCompareReal` 只需在中间加一步「slv → real」的解析。下面是**示例代码**（项目原本没有，按既有风格编写）：

```vhdl
-- 示例代码：仿照 psi_tb_compare_pkg 风格新增的定点比较过程
-- 解析规则：Actual 为无符号定点，整数部分 Bits-1..FrcBits，小数部分 FrcBits-1..0
procedure StdlvCompareReal(Expected  : in real;
                           Actual    : in std_logic_vector;
                           Bits      : in integer;        -- 总位宽
                           FrcBits   : in integer;        -- 小数部分位宽
                           Msg       : in string;
                           Tolerance : in real   := 0.0;
                           Prefix    : in string  := "###ERROR###: ") is
    variable IntPart_v : integer;
    variable FrcReal_v : real;
    variable ActReal_v : real;
begin
    -- 1) 把 std_logic_vector 按"无符号定点"解析为实数
    IntPart_v := to_integer(unsigned(Actual(Bits-1 downto FrcBits)));
    FrcReal_v := real(to_integer(unsigned(Actual(FrcBits-1 downto 0)))) / (2.0**FrcBits);
    ActReal_v := real(IntPart_v) + FrcReal_v;
    -- 2) 与 RealCompare 完全一致的 assert/report 骨架
    assert (ActReal_v >= Expected - Tolerance) and (ActReal_v <= Expected + Tolerance)
    report Prefix & Msg &
           " [Expected " & to_string(Expected) &
           ", Received " & to_string(ActReal_v) &
           ", Tolerance " & to_string(Tolerance) & "]"
    severity error;
end procedure;
```

它满足 4.4.1 的全部约定：

- `Prefix` 默认 `"###ERROR###: "`，与 CI 契约一致。
- `Tolerance` 默认 `0.0`，类型为 `real`。
- 断言骨架与 `RealCompare` 逐字相同，只是 `Actual` 换成了本地算出的 `ActReal_v`。
- 字符串渲染用 `txt_util` 的 `to_string`（real 重载内部即 `real'image`，见 [hdl/psi_tb_txt_util.vhd:L362-L365](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L362-L365)）。
- 它本身不调更低的层，但和整条复用链共享同一个 `Prefix` 约定，失败时一样会被 `run_check_errors "###ERROR###"` 抓到。

声明（package header）部分按现有风格在 `package psi_tb_compare_pkg is` 与 `end;` 之间加一行即可，位置可紧挨 `RealCompare` 声明之后（参考 [hdl/psi_tb_compare_pkg.vhd:L58-L62](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L58-L62)）。

#### 4.4.4 代码实践

这是本讲的主实践任务，目标是把 `StdlvCompareReal` 真正写出来并跑通一个最小验证。

1. 实践目标：新增 `StdlvCompareReal`，写一个 TB 同时覆盖「通过」与「失败」两个用例，确认失败用例打印 `###ERROR###`。
2. 操作步骤：
   - 把 4.4.3 的声明与实现加进本地副本的 `hdl/psi_tb_compare_pkg.vhd`（**不要改仓库源码做提交，仅在本地学习副本上练习**）。
   - 新建一个最小 TB（示例代码如下）。
3. 需要观察的现象：
   - 通过用例（期望 `9.56`，容差 `0.01`，实际 `9.5625`）不打印任何 `###ERROR###`。
   - 失败用例（期望 `10.0`，容差 `0.01`）打印一行 `###ERROR###: 定点值故意不匹配 [Expected 1.0E+1, Received 9.5625, Tolerance 1.0E-2]`（具体 `real'image` 格式因仿真器而异）。
4. 预期结果：Transcript 里**恰好出现一处** `###ERROR###`，来自失败用例；通过用例静默。仿真结束后 `run_check_errors` 应报错。
5. 实际跑仿真需本地 PsiSim 环境；若无，标注「待本地验证」，可先靠静态阅读确认断言骨架与 `RealCompare` 一致。

最小 TB 示例代码（项目原本没有，按既有风格编写）：

```vhdl
-- 示例代码：StdlvCompareReal 的最小验证 TB
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
library work;
use work.psi_tb_txt_util.all;
use work.psi_tb_compare_pkg.all;    -- 假设 StdlvCompareReal 已加入此包

entity slv_compare_real_tb is
end entity;

architecture sim of slv_compare_real_tb is
begin
    process
        -- 8 位、4 位小数：10011001 = 9 + 9/16 = 9.5625
        constant Data_c : std_logic_vector(7 downto 0) := "10011001";
    begin
        -- 通过用例：9.5625 落在 [9.55, 9.57] 内
        StdlvCompareReal(9.56, Data_c, 8, 4, "定点值匹配", 0.01);
        -- 失败用例：故意期望 10.0，触发 ###ERROR###
        StdlvCompareReal(10.0, Data_c, 8, 4, "定点值故意不匹配", 0.01);
        wait;
    end process;
end architecture;
```

#### 4.4.5 小练习与答案

**练习 1**：如果要支持有符号定点（负数），`StdlvCompareReal` 应该改哪里？

**参考答案**：仿照 `StdlvCompareInt` 的 `IsSigned` 参数，加一个 `IsSigned : in boolean := false`。当为 `true` 时，整数部分改用 `to_integer(signed(...))` 解析，并相应处理符号位。更稳妥的做法是参照 `SignCompare` / `SignCompare2` 的差异（见 u3-l2）。

**练习 2**：为什么示例代码里用 `to_string(ActReal_v)` 而不能用 `integer'image(ActReal_v)`？

**参考答案**：`ActReal_v` 是 `real` 类型，`integer'image` 只接受 `integer`，类型不匹配会编译失败。`txt_util` 提供了 `to_string(num : real)` 重载（内部即 `real'image`），这才是渲染实数的正确函数。

**练习 3**：`StdlvCompareReal` 的声明应该加进哪个文件、为什么？

**参考答案**：加进 `hdl/psi_tb_compare_pkg.vhd`。因为它是「值检查函数」，正是 README 列为应收纳的类别；且与现有的 `RealCompare` / `StdlvCompareInt` 同属一类，放在一起最符合「一包一文件、按职能归类」的约定。

## 5. 综合实践

把 4.4 的成果接进 CI 流水线，验证它真的能被自动化判定。这个任务贯穿本讲全部要点：新增过程（4.4）遵守统一前缀（4.1）、坐在 compare 包里成为复用链一员（4.2）、保持消息格式一致（4.3），最终被 CI 通过 `###ERROR###` 抓住（4.1）。

任务步骤：

1. 在本地学习副本上，把 `StdlvCompareReal` 的声明与实现加进 `hdl/psi_tb_compare_pkg.vhd`（紧邻 `RealCompare`）。
2. 把 4.4.4 的最小 TB 存为 `testbench/slv_compare_real_tb.vhd`。
3. 在 [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl) 里做两处改动（参照现有 I2C TB 的注册方式，[sim/config.tcl:L36-L42](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L36-L42)）：
   - 在 `-tag tb` 的 `add_sources` 列表里追加 `slv_compare_real_tb.vhd \`。
   - 在末尾追加一对 `create_tb_run "slv_compare_real_tb"` 与 `add_tb_run`。
4. 跑 `sim/run.tcl`（ModelSim）或 `sim/runGhdl.tcl`（GHDL）。

预期结果与判定：

- **当前 TB 含一个故意失败的用例**，因此 Transcript 应出现一行 `###ERROR###: 定点值故意不匹配 ...`，`run_check_errors "###ERROR###"`（[sim/run.tcl:L32](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl#L32)）应让 CI 判定为**失败**。这正面验证了「统一前缀 → CI 失败」的契约。
- 随后把失败用例的期望值从 `10.0` 改回 `9.56`（或扩大容差），重跑，Transcript 不再出现 `###ERROR###`，CI 判定为**通过**。

这组「红→绿」切换，完整演示了 psi_tb 的错误消息机制如何从一行 `report` 一路传导到 CI 退出码。若本地无仿真环境，上述运行结果标注「待本地验证」，可先通过静态阅读确认：TB 的失败用例确实会触发 `assert` 为 `false` → 打印 `Prefix & Msg & ...` → `Prefix` 默认值与 `run.tcl` 扫描串一致。

## 6. 本讲小结

- psi_tb 全库与 CI 之间靠 **`###ERROR###` 这个字面量**咬合：过程用 `Prefix : in string := "###ERROR###: "` 默认值拼消息，`run.tcl` 用 `run_check_errors "###ERROR###"` 扫同一个串。判定靠子串、不靠 `severity`，所以全库用 `severity error`（只打印、不中断）以收集全部错误。
- 库是一条 **`txt_util → compare → activity → i2c/axi/...` 的分层复用链**，层间靠 `use work.xxx.all` 决定编译顺序（与 `config.tcl` 的 `-tag src` 顺序一致），层内靠普通过程调用复用低层断言；上层把 `Prefix` 原样透传给下层，保证消息风格统一。
- 比较包里所有过程共享同一套 **`assert (容差带) report Prefix & Msg & "[Expected..., Received..., Tolerance...]" severity error`** 骨架，`SignCompareInt` / `UsignCompareInt` 还示范了「薄包装」复用模式。
- I2C 包用 **`MsgInfo_r` record + `GenMessage` / `GenMessageNoPrefix`** 做结构化消息：直接 `report` 时用 `GenMessage`（带前缀）；复用 `CheckLastActivity` 时用 `GenMessageNoPrefix`（剥前缀，避免前缀重复）。
- 二次开发有明确清单：加进功能最贴近的现有 package、保留 `Prefix` 默认值、沿用断言骨架、用 `txt_util` 渲染字符串、优先复用低层过程。本讲按此清单实现了 `StdlvCompareReal`，并接入 CI 验证「红→绿」切换。
- 这些约定并非强制语法，而是让「一个写的检查过程」自动获得「可读消息 + CI 自动判定 + 与全库一致风格」三重收益的工程纪律。

## 7. 下一步学习建议

本讲已结束 psi_tb 手册的全部内容。接下来可以朝三个方向继续：

1. **动手扩展**：按 4.4 的清单，给 `psi_tb_compare_pkg` 补一个有符号版的 `StdlvCompareReal`（加 `IsSigned`），或给 `psi_tb_activity_pkg` 补一个「带超时的 `ClockedWaitFor`」（解决 u4-2 提到的可能永久挂起问题）。这是检验你是否真的理解本讲约定的最好方式。
2. **横向对照**：阅读 psi_common 的可综合 AXI 包（`psi_common_axi_pkg`），对比它与 psi_tb 的 `axi_ms_r` / `axi_sm_r` 在打包方式上的差异（详见 u5-4），理解「综合包」与「testbench 包」为何要分开维护。
3. **回到整车**：以 [testbench/psi_tb_i2c_pkg_tb.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd) 为模板，写一个属于自己的 DUT 的 testbench——用 `axi_single_expect` 驱地址、用 `CheckNoActivity` 查空闲、用本讲新增的 `StdlvCompareReal` 查定点输出，最后让 CI 的 `run_check_errors` 给出判决。当你能独立完成这一步，就真正出师了。
