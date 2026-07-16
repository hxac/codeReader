# olo_fix_pkg 与字符串泛型模式

## 1. 本讲目标

本讲聚焦 Open Logic `fix` 区域的公共包 `olo_fix_pkg`。读完本讲后，你应该能够：

- 说出 `olo_fix_pkg` 在 fix 区域中的「翻译层」定位，以及它为何必须紧跟 `en_cl_fix` 编译。
- 列出包里为 `FixRound_t` / `FixSaturate_t` 提供的全部字符串常量，并知道它们的拼写从何而来。
- 解释「字符串泛型」的完整解析链路：接口上的 `string` 如何在实体内部被还原成带类型的常量、又如何用来推导端口位宽。
- 理解舍入/饱和/寄存器三组泛型的默认值约定，特别是 `"YES"` / `"NO"` / `"AUTO"` 三态在 `fixImplementReg` 里的判定逻辑。
- 在实例化一个 `olo_fix` 实体时，用至少四种等价写法正确传递定点格式与舍入模式字符串。

## 2. 前置知识

本讲承接 [u8-l1 定点原理与 en_cl_fix 基础](u8-l1-fix-principles-enclfix.md)，默认你已经知道：

- **定点格式三元组 `(S,I,F)`**：S 为符号位、I 为整数位、F 为小数位，位宽 \( W = S + I + F \)，对应 en_cl_fix 的 `FixFormat_t`（一个 record）。
- **`FixRound_t` / `FixSaturate_t`**：en_cl_fix 用枚举分别表示舍入模式（如 `Trunc_s`、`NonSymPos_s`）和饱和模式（如 `None_s`、`Sat_s`）。
- **字符串泛型模式**：为了同时支持 VHDL 与 Verilog 实例化，`olo_fix_*` 实体的接口不用自定义类型，而用 `string`（如 `"(1,8,23)"`、`"Trunc_s"`）。
- **三段式基本运算**：operation → round → saturate，段间可插流水线寄存器。

此外需要一点 VHDL 常识：泛型（generic）在**实例化时**给定、端口位宽可在端口声明中调用**编译期函数**推导；`constant` 在架构区（architecture）一经声明即不可变。

> 本环境提示：本仓库的 `3rdParty/en_cl_fix` 是一个 git 子模块，在此沙箱中**未检出**（目录为空）。因此本讲凡涉及 `cl_fix_*` 系列函数（如 `cl_fix_format_from_string`、`cl_fix_round_from_string`、`cl_fix_width`），均依据 Open Logic 对它们的**实际调用方式**来描述，不臆测其内部实现。你在本地用 `git clone --recursive` 检出后即可读到完整源码。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/fix/vhdl/olo_fix_pkg.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_pkg.vhd) | 本讲主角：定义字符串常量、`fixFmtWidthFromString`、`fixImplementReg` 等翻译/解析函数。 |
| [src/fix/vhdl/olo_fix_resize.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_resize.vhd) | 截位/重格式化实体，作为「字符串泛型如何声明与解析」的典型样本。 |
| [src/fix/vhdl/olo_fix_round.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_round.vhd) | 舍入实体，展示默认值差异与 `fixImplementReg` 的真实调用。 |
| [test/fix/olo_fix_resize/olo_fix_resize_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_resize/olo_fix_resize_tb.vhd) | resize 的 VUnit 测试台，演示用字符串泛型参数化测试用例。 |
| [compile_order.txt](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/compile_order.txt) | 编译顺序，证明 `olo_fix_pkg` 紧随 `en_cl_fix` 之后、先于所有 `olo_fix_*` 实体。 |
| [doc/fix/olo_fix_pkg.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_pkg.md) / [doc/fix/olo_fix_principles.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_principles.md) | 官方文档：常量清单与字符串泛型动机说明。 |

## 4. 核心概念与源码讲解

### 4.1 公共字符串选项：把枚举「翻译」成字符串常量

#### 4.1.1 概念说明

en_cl_fix 用 VHDL 枚举表示舍入与饱和模式，例如 `Trunc_s`、`NonSymPos_s`、`Sat_s`。这些符号名在 VHDL 里很优雅，但有一个致命问题：**Verilog 实例化 VHDL 时，无法可靠地传递 VHDL 自定义枚举类型**（各工具支持程度不一，没有共同子集）。

`olo_fix_pkg` 解决这件事的办法很朴素但很有效：**把每个枚举值的「字面拼写」原封不动地存成一个 `string` 常量**。于是：

- 在 VHDL 里，你可以写 `Round_g => FixRound_Trunc_c`；
- 在 Verilog 里，你写 `Round_g => "Trunc_s"`；
- 两者传到实体内部的字符串**完全相同**，实体据此做后续解析。

这样，包里的字符串常量就成了「跨语言单一拼写真相源」——既避免手敲字符串拼错，也让代码自文档化。

#### 4.1.2 核心流程

```text
en_cl_fix 枚举字面量          olo_fix_pkg 字符串常量            实体泛型（接口）
Trunc_s          ──┐
NonSymPos_s        ├──>  FixRound_Trunc_c := "Trunc_s"   ──>  Round_g : string
SymInf_s           └──>  FixRound_SymInf_c := "SymInf_s"
                          ...
None_s / Sat_s / ...     FixSaturate_Sat_c := "Sat_s"     ──>  Saturate_g : string
```

关键点：常量值就是枚举字面量加一对引号，没有任何额外变换——这正是 en_cl_fix 内部字符串解析函数能认出它的原因。

#### 4.1.3 源码精读

包头的字符串常量集中成两块。[olo_fix_pkg.vhd:43-54](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_pkg.vhd#L43-L54) 定义了全部舍入与饱和字符串常量，值就是带引号的枚举名：

- `FixRound_Trunc_c .. FixRound_ConvOdd_c`（7 个舍入模式，对应 en_cl_fix 的 7 个 `FixRound_t` 字面量）。
- `FixSaturate_None_c .. FixSaturate_SatWarn_c`（4 个饱和模式）。

此外，[olo_fix_pkg.vhd:56](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_pkg.vhd#L56) 定义了一个**非字符串**的兜底格式常量 `FixFmt_Unused_c := (0,1,0)`，并注释「未用格式必须保留 1 位以防问题」——它被容错解析函数当作「解析失败」的默认返回值（见 4.2）。

实体侧的典型用法见 [olo_fix_resize.vhd:40-41](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_resize.vhd#L40-L41)：`Round_g` 与 `Saturate_g` 的默认值直接引用包里的字符串常量 `FixRound_Trunc_c` / `FixSaturate_Warn_c`，而不是裸字符串。

#### 4.1.4 代码实践

1. **实践目标**：亲手从源码里把所有公共字符串选项抄一遍，建立「枚举名 ↔ 字符串常量」的肌肉记忆。
2. **操作步骤**：
   - 打开 [olo_fix_pkg.vhd:43-54](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_pkg.vhd#L43-L54)。
   - 建一张三列表格：**枚举字面量** | **olo_fix_pkg 常量名** | **字符串值**。例如第一行：`Trunc_s` | `FixRound_Trunc_c` | `"Trunc_s"`。
   - 对照 [doc/fix/olo_fix_pkg.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_pkg.md) 的 *String Representations* 两节，给每个舍入/饱和模式补一句中文解释。
3. **需要观察的现象**：常量名的前缀（`FixRound_` / `FixSaturate_`）与值的前缀（`Trunc_s` 等的语义类别）一一对应；舍入有 7 个、饱和有 4 个。
4. **预期结果**：得到 7+4 共 11 行的表格，且每一行的「字符串值」剥掉引号后都正好是 en_cl_fix 的枚举字面量。
5. 待本地验证（纯阅读型实践，无需仿真）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `olo_fix_pkg` 要为这些选项提供字符串常量，而不是让用户直接写 `"Trunc_s"`？
**答案**：常量是「拼写单一真相源」，避免手敲字符串拼错（如 `"Trunc_S"` 大小写错）；同时自文档化——`FixRound_Trunc_c` 比裸字符串 `"Trunc_s"` 更易读，IDE 也能补全。

**练习 2**：`FixSaturate_Warn_c` 与 `FixSaturate_SatWarn_c` 的语义区别是什么？
**答案**：`Warn_s`（`FixSaturate_Warn_c`）**不增加饱和逻辑**，仅在仿真中遇到溢出回绕时告警；`SatWarn_s`（`FixSaturate_SatWarn_c`）**增加饱和逻辑且**在发生饱和时告警。前者用于「不希望悄悄饱和、想发现设计错误」的场景，后者用于「饱和是预期行为、但仍想知道它发生了」的场景。

---

### 4.2 泛型解析：字符串在实体内部如何「变回」类型与位宽

#### 4.2.1 概念说明

接口上传递的是字符串，但实体内部的 RTL 逻辑需要两样**带类型**的东西：

1. **类型化常量**：`FixFormat_t`（record）、`FixRound_t`（枚举），用于调用 en_cl_fix 的运算函数。
2. **端口位宽**：端口 `In_A` / `Out_Result` 的 `std_logic_vector` 范围必须由格式推导出来。

这两件事都发生在**编译期/细化期（elaboration）**，因此需要编译期函数把字符串「翻译」回去。`olo_fix_pkg` 在这里承担翻译职责：它把 en_cl_fix 的字符串解析函数再包一层，使其更贴合 `olo_fix_*` 实体的使用习惯。

#### 4.2.2 核心流程

```text
                  ┌──────────────── 接口（generic）─────────────────┐
AFmt_g : string "(1,8,23)"  ──┐
                              │
        ┌─────────────────────┴──────────────────────┐
        ▼                                            ▼
  [端口位宽] fixFmtWidthFromString(AFmt_g)      [内部常量] cl_fix_format_from_string(AFmt_g)
        │                                            │
        ▼                                            ▼
  natural (位宽 W)                            FixFormat_t record (S,I,F)
  用于 std_logic_vector(W-1 downto 0)         用于调用 cl_fix_resize / cl_fix_round ...
```

位宽推导的数学关系即 \( W = S + I + F \)。`fixFmtWidthFromString` 内部先用 en_cl_fix 的解析函数拿到 `(S,I,F)`，再调 `cl_fix_width` 求和。

#### 4.2.3 源码精读

**位宽推导函数**：[olo_fix_pkg.vhd:118-122](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_pkg.vhd#L118-L122)。`fixFmtWidthFromString` 只有两行实质逻辑：先把字符串转成 `FixFormat_t` 常量 `FixFmt_c`，再返回 `cl_fix_width(FixFmt_c)`。

**端口处直接调用该函数**：[olo_fix_resize.vhd:52](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_resize.vhd#L52) 把端口宽度写成 `fixFmtWidthFromString(AFmt_g) - 1 downto 0`。注意这是在**端口声明**里调用函数——合法，因为泛型 `AFmt_g` 在端口确定前就已经可见。

**架构区把字符串还原成类型**：[olo_fix_resize.vhd:62-64](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_resize.vhd#L62-L64) 用 `cl_fix_format_from_string(AFmt_g)` 得到 `AFmt_c : FixFormat_t`，用 `cl_fix_round_from_string(Round_g)` 得到 `Round_c : FixRound_t`。这两个 `cl_fix_*_from_string` 函数来自 en_cl_fix（见 [olo_fix_round.vhd:61-63](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_round.vhd#L61-L63) 的同款写法）。

**容错变体**：[olo_fix_pkg.vhd:158-199](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_pkg.vhd#L158-L199) 的 `fixFmtFromStringTolerant` 用一个状态机先扫描字符串是否具备 `( , , )` 的括号结构；只有结构完整（`State_v = Done_s`）才真正解析，否则返回 `FixFmt_Unused_c`（即 `(0,1,0)`，宽度 1）。对应的位宽版 [olo_fix_pkg.vhd:124-128](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_pkg.vhd#L124-L128)。这种容错用于某些格式「可能未用/可能为空」的场合，避免非法字符串直接让 elaboration 失败。

> 旁注：包里还有 [fixDynShift](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_pkg.vhd#L201-L235)（用 `for` 循环把变移位变成常量移位以讨好综合工具）与一组 `fixFileRead*` 文件读取函数（配合协仿真，详见 u8-l5）。它们与「字符串泛型」无直接关系，但同属这个「en_cl_fix 桥接包」的职责。

#### 4.2.4 代码实践

1. **实践目标**：验证「改字符串即改位宽」，理解端口宽度确实由泛型字符串推导。
2. **操作步骤**：
   - 阅读 [olo_fix_resize.vhd:36-56](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_resize.vhd#L36-L56)，确认 `AFmt_g` / `ResultFmt_g` 都是 `string`、无默认值（必填）。
   - 在测试台 [olo_fix_resize_tb.vhd:35-36](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_resize/olo_fix_resize_tb.vhd#L35-L36) 里看默认 `AFmt_g = "(1,15,0)"`、`ResultFmt_g = "(0,1,8)"`，手算：输入位宽 \( 1+15+0=16 \)、输出位宽 \( 0+1+8=9 \)。
   - 再看 [olo_fix_resize_tb.vhd:61](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_resize/olo_fix_resize_tb.vhd#L61) 的信号 `In_A` 宽度正是 `fixFmtWidthFromString(AFmt_g)-1 downto 0`，与手算一致。
3. **需要观察的现象**：把 `AFmt_g` 改成 `"(1,7,0)"`，`In_A` 的宽度应随之变成 8。
4. **预期结果**：位宽始终等于三元组之和，证明端口宽度是「字符串 → 解析 → 求和」在 elaboration 期算出来的。
5. 待本地验证（若本地已检出 en_cl_fix，可直接编译观察；否则属源码阅读型推导）。

#### 4.2.5 小练习与答案

**练习 1**：`fixFmtWidthFromString("(0,3,4)")` 返回多少？
**答案**：\( 0+3+4=7 \)。

**练习 2**：为什么端口声明里可以直接调用 `fixFmtWidthFromString(AFmt_g)`，而不必先在架构里算好？
**答案**：VHDL 中泛型在端口可见之前就已确定，且 `fixFmtWidthFromString` 是纯编译期函数（其内部只调 `cl_fix_format_from_string` 与 `cl_fix_width`，无副作用），因此可在端口范围表达式中使用，由 elaboration 期求值。

**练习 3**：`fixFmtFromStringTolerant("(1,8")`（少一个逗号与右括号）会返回什么？
**答案**：状态机扫描不到完整的 `( , , )` 结构，`State_v` 停在 `IntBits_s` 而非 `Done_s`，于是返回初值 `FixFmt_Unused_c`，即 `(0,1,0)`。

---

### 4.3 默认值约定：舍入/饱和/寄存器三组默认

#### 4.3.1 概念说明

Open Logic 的基本运算实体把配置分成三类泛型，且都预设了「最常用」的默认值，让你只填格式、其余留空也能跑起来：

- **格式类**（`AFmt_g` / `ResultFmt_g`）：**无默认值，必填**——格式是设计核心，必须显式给出。
- **舍入/饱和类**（`Round_g` / `Saturate_g`）：有默认值，默认值因实体而异。
- **寄存器类**（`RoundReg_g` / `SatReg_g`）：默认 `"YES"`，并可取 `"YES"` / `"NO"` / `"AUTO"` 三态。

寄存器三态的含义（见 [olo_fix_principles.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_principles.md) 的 *Pipeline Registers* 节）：`"YES"` 无条件插寄存器（固定延迟、最高主频，是默认）；`"NO"` 不插（低延迟但组合路径长）；`"AUTO"` 仅在该段确实存在舍入/饱和逻辑时才插。

#### 4.3.2 核心流程

`"AUTO"` 的判定由 `olo_fix_pkg` 的内部函数 `fixImplementReg` 完成：

```text
fixImplementReg(logicPresent, regMode):
  regMode = "YES"  -> 返回 true   (无论如何都插)
  regMode = "NO"   -> 返回 false  (无论如何都不插)
  regMode = "AUTO" -> 返回 logicPresent  (有逻辑才插)
  其它             -> 仿真断言 failure (综合期被 translate_off 屏蔽)
```

其中 `logicPresent` 由实体自己判断——例如舍入段「有逻辑」当且仅当输入小数位多于输出小数位（`AFmt_c.F > ResultFmt_c.F`）。

#### 4.3.3 源码精读

**默认值差异**：同样是 `Round_g`，[olo_fix_resize.vhd:40](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_resize.vhd#L40) 默认 `FixRound_Trunc_c`（截位是 resize 的常见意图），而 [olo_fix_round.vhd:40](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_round.vhd#L40) 默认 `FixRound_NonSymPos_c`（四舍五入更符合 round 的直觉）。这说明默认值是「按实体语义量身定做」的，不是全库一刀切。

**寄存器默认 `"YES"`**：[olo_fix_resize.vhd:43-44](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_resize.vhd#L43-L44) 的 `RoundReg_g` / `SatReg_g` 均默认 `"YES"`。

**`fixImplementReg` 实现**：[olo_fix_pkg.vhd:130-156](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_pkg.vhd#L130-L156) 用 `compareNoCase` 做大小写不敏感比较（所以 `"yes"` / `"Yes"` / `"YES"` 等价），三态分别返回 `true` / `false` / `logicPresent`；非法值在仿真中 `assert ... severity failure`，并被 `-- synthesis translate_off` 包住以免影响综合。比较函数 `compareNoCase` 来自 base 区的 `olo_base_pkg_string`（[olo_base_pkg_string.vhd:35-37](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_string.vhd#L35-L37)）。

**实体里的调用链**：[olo_fix_round.vhd:66-68](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_round.vhd#L66-L68) 是 `AUTO` 模式的范本——先算 `LogicPresent_c := AFmt_c.F > ResultFmt_c.F`，再 `ImplementReg_c := fixImplementReg(LogicPresent_c, RoundReg_g)`，最后 `OpRegStages_c := choose(ImplementReg_c, 1, 0)` 把布尔转成寄存器级数。

#### 4.3.4 代码实践

1. **实践目标**：跟踪 `RoundReg_g => "AUTO"` 时寄存器是否插入的判定路径。
2. **操作步骤**：
   - 假设实例化 `olo_fix_round`，`AFmt_g => "(1,8,4)"`、`ResultFmt_g => "(1,8,8)"`、`RoundReg_g => "AUTO"`。
   - 跟着 [olo_fix_round.vhd:62-68](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_round.vhd#L62-L68) 走：`AFmt_c.F=4`、`ResultFmt_c.F=8`，`LogicPresent_c := 4 > 8 = false`，`fixImplementReg(false,"AUTO")` 返回 `false`，故 `OpRegStages_c=0`，**不插寄存器**。
   - 再把 `ResultFmt_g` 改成 `"(1,8,2)"`：`4 > 2 = true`，`fixImplementReg(true,"AUTO")` 返回 `true`，**插一级寄存器**。
3. **需要观察的现象**：同样的 `"AUTO"`，因为「是否真有舍入逻辑」不同，寄存器数量从 0 变 1。
4. **预期结果**：`"AUTO"` = 「按需插寄存器」，延迟随格式组合变化；而 `"YES"` 无论有无逻辑都插一级。
5. 待本地验证（源码阅读型跟踪；本地检出后可用 `--coverage` 仿真观察 `fixImplementReg` 各分支命中情况）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `RoundReg_g` 的默认值是 `"YES"` 而不是 `"AUTO"`？
**答案**：`"YES"` 保证**延迟固定**（与格式组合无关）且主频最高，这在定点 DSP/视频等典型应用里往往最关键；`"AUTO"` 虽省掉无谓延迟，但延迟会随格式变化，时序与延迟对齐更难预测。官方因此把「快但固定」的 `"YES"` 作为默认。

**练习 2**：`fixImplementReg(true, "no")` 返回什么？为什么大小写没关系？
**答案**：返回 `false`。因为内部用 `compareNoCase` 做比较，`"no"` / `"NO"` / `"No"` 都被判为相等，直接走 `regMode = "NO"` 分支返回 `false`，与 `logicPresent` 无关。

---

### 4.4 实例化传递：四种写法与 `to_string` 桥接

#### 4.4.1 概念说明

理解了「常量、解析、默认值」之后，落到工程实践：实例化一个 `olo_fix` 实体时，定点格式与舍入模式到底怎么传？Open Logic 提供了**四种等价写法**，背后传到实体的都是同一个字符串：

1. **裸字符串字面量**：`AFmt_g => "(1,8,23)"`。最直白，但拼错不报错（直到 elaboration）。
2. **包字符串常量**（仅舍入/饱和）：`Round_g => FixRound_Trunc_c`。推荐，自文档化。
3. **`to_string` 桥接**（VHDL 专属）：`AFmt_g => to_string(MyFmt_c)`，其中 `MyFmt_c : FixFormat_t`。适合你已在 VHDL 里用 en_cl_fix 类型变量做过计算、想直接喂给实体的场景。
4. **Verilog `localparam string`**：`localparam string fmt = "(1,8,23)";` 然后 `AFmt_g => fmt`。跨语言实例化的标准姿势。

#### 4.4.2 核心流程

```text
写法 1: "(1,8,23)"            ─┐
写法 2: FixRound_Trunc_c       │  实体内部都用 cl_fix_*_from_string 解析
写法 3: to_string(MyFmt_c)     │  → 得到相同的 (S,I,F) / FixRound_t
写法 4: localparam string fmt  ─┘
```

`to_string` 是 en_cl_fix 为 `FixFormat_t` / `FixRound_t` / `FixSaturate_t` 提供的序列化函数，把类型变回字符串——它是 VHDL 用户在「类型化代码」与「字符串接口」之间的桥梁。注意它**只在 VHDL 侧可用**，Verilog 没有 en_cl_fix 类型，只能用写法 1/4。

#### 4.4.3 源码精读

**`to_string` 桥接在实体内部的典型应用**：`olo_fix_resize` 把上游算出的中间格式 `RoundFmt_c`（一个 `FixFormat_t`）传给下游子实体时，用 `to_string(RoundFmt_c)` 转回字符串——见 [olo_fix_resize.vhd:85](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_resize.vhd#L85) 与 [olo_fix_resize.vhd:101](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_resize.vhd#L101)。这是「实体套实体」时传递计算结果的惯用法。

**测试台里的裸字符串写法**：[olo_fix_resize_tb.vhd:35-40](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_resize/olo_fix_resize_tb.vhd#L35-L40) 把所有泛型都用裸字符串/常量给定：`AFmt_g => "(1,15,0)"`、`Round_g => "NonSymPos_s"`、`RoundReg_g => "YES"` 等。注意 TB 故意用裸字符串 `"NonSymPos_s"` 而非 `FixRound_NonSymPos_c`——因为 TB 要验证「字符串接口本身」能被正确解析。

**官方推荐写法对照**：[doc/fix/olo_fix_pkg.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_pkg.md) 给出两行对照——`Round_g => FixRound_Trunc_c`（用包常量）与 `Round_g => to_string(Trunc_s)`（用 en_cl_fix 枚举再转字符串），二者等价。

> 一致性约束：当多个实体串成数据通路时，**相邻实体的格式与舍入/饱和设置必须匹配**。例如上游输出格式必须等于下游 `AFmt_g`；若你在某段关掉了舍入（`Trunc_s`）而在另一段期望四舍五入，结果会与 Python 位真模型不符。这与 [u5-l3 CRC](u5-l3-crc-engine.md) 里「append 与 check 必须用相同 CRC 参数」是同一类约束。

#### 4.4.4 代码实践

1. **实践目标**：写出一个 `olo_fix_resize` 的实例化片段，用字符串同时传递格式与舍入模式，并尝试两种写法对比。
2. **操作步骤**：
   - 假设你要把 `(1,8,4)` 的定点数截位成 `(1,8,0)`，采用截断舍入、关闭饱和告警。写下实例化：

     ```vhdl
     -- 示例代码：用户侧实例化 olo_fix_resize
     library olo;
         use olo.olo_fix_pkg.all;        -- 引入 FixRound_Trunc_c / FixSaturate_None_c
     ...
     i_resize : entity olo.olo_fix_resize
         generic map (
             AFmt_g      => "(1,8,4)",            -- 写法 1：裸字符串
             ResultFmt_g => "(1,8,0)",
             Round_g     => FixRound_Trunc_c,     -- 写法 2：包字符串常量
             Saturate_g  => FixSaturate_None_c,
             RoundReg_g  => "YES",                -- 默认值，可省略
             SatReg_g    => "YES"
         )
         port map (
             Clk        => Clk,
             Rst        => Rst,
             In_Valid   => DataValid,
             In_A       => DataIn,                -- 13 位 (1+8+4)
             Out_Valid  => DataValidOut,
             Out_Result => DataOut                -- 9 位 (1+8+0)
         );
     ```

   - 再写一份等价的「`to_string` 桥接」版（仅 VHDL）：把格式先声明成 `FixFormat_t` 常量，再 `to_string`：

     ```vhdl
     -- 示例代码：用 to_string 把类型化常量桥接到字符串接口
     use olo.en_cl_fix_pkg.all;
     ...
     constant InFmt_c  : FixFormat_t := (1,8,4);
     constant OutFmt_c : FixFormat_t := (1,8,0);
     ...
     i_resize2 : entity olo.olo_fix_resize
         generic map (
             AFmt_g      => to_string(InFmt_c),   -- 写法 3：to_string 桥接
             ResultFmt_g => to_string(OutFmt_c),
             Round_g     => to_string(Trunc_s)    -- 枚举值再转字符串
         ) ...
     ```

3. **需要观察的现象**：两份代码 elaboration 后，实体内部 `AFmt_c` 完全相同；输入端口 `In_A` 都是 13 位、输出 `Out_Result` 都是 9 位。
4. **预期结果**：三种写法（裸字符串、包常量、`to_string`）功能完全等价，区别只在可读性与是否依赖 en_cl_fix 类型。
5. 待本地验证（需检出 en_cl_fix 子模块后才能编译；本环境子模块为空，故为源码阅读型实践）。

#### 4.4.5 小练习与答案

**练习 1**：从 Verilog 实例化 `olo_fix_resize`，`Round_g` 应该怎么传？
**答案**：Verilog 没有 `FixRound_t` 枚举，也不能用 `olo_fix_pkg` 的 VHDL 常量，只能传字符串字面量，例如 `Round_g => "Trunc_s"`（或先 `localparam string round = "Trunc_s";` 再传）。这正是接口用 `string` 的根本动机。

**练习 2**：`olo_fix_resize` 内部把 `RoundFmt_c` 传给子实体时用了 `to_string(RoundFmt_c)`（[olo_fix_resize.vhd:85](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_resize.vhd#L85)）。为什么不直接传 `RoundFmt_c`？
**答案**：因为子实体 `olo_fix_round` / `olo_fix_saturate` 的 `AFmt_g` 是 `string` 类型接口（为了 Verilog 兼容）。`RoundFmt_c` 是 `FixFormat_t` record，不能直接赋给 `string` 泛型，必须经 `to_string` 序列化回 `"(S,I,F)"` 形式。

**练习 3**：`AFmt_g` / `ResultFmt_g` 为什么没有默认值，而 `Round_g` 有？
**答案**：格式是设计核心参数，不存在合理的「万能默认」，必须由用户显式给出；舍入/饱和模式则有公认的常用值（截断/告警），给默认值能减少样板代码、让简单用例更简洁。

## 5. 综合实践

把本讲四个最小模块串起来，完成一个小设计：

**任务**：用 `olo_fix_resize` 把一个 `(1,7,8)`（16 位）的定点数重格式化为 `(0,1,7)`（8 位），要求：

1. 在你的 VHDL 设计里**先声明类型化常量** `InFmt_c : FixFormat_t := (1,7,8)` 与 `OutFmt_c : FixFormat_t := (0,1,7)`，再用 `to_string` 桥接传给实体（模块 4.4）。
2. 舍入用**包字符串常量** `FixRound_NonSymPos_c`，饱和用 `FixSaturate_Sat_c`（模块 4.1）。
3. 寄存器模式设为 `RoundReg_g => "AUTO"`、`SatReg_g => "YES"`，并**手算跟踪** `fixImplementReg` 在舍入段的返回值（模块 4.3）：`InFmt_c.F=8`、`OutFmt_c.F=7` ⇒ `LogicPresent = 8>7 = true` ⇒ `AUTO` 下插一级寄存器。
4. 解释实体的输入端口为何是 16 位、输出为何是 8 位（模块 4.2：\( W=S+I+F \)）。

**交付物**：一份实例化 VHDL 片段 + 一段说明，指出每个泛型用了四种写法中的哪一种、`AUTO` 分支的判定结果、以及端口位宽的推导过程。

> 本环境子模块未检出，故本任务以「写代码 + 手算推导」为主；本地检出 en_cl_fix 后，可进一步用 `olo_fix_cosim`（见 [u8-l5](u8-l5-cosimulation.md)）生成位真期望并验证。

## 6. 本讲小结

- `olo_fix_pkg` 是 fix 区域的「翻译层/桥接包」，紧跟 `en_cl_fix` 之后编译，先于所有 `olo_fix_*` 实体（[compile_order.txt:56-58](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/compile_order.txt#L56-L58)）。
- 它把 en_cl_fix 的 `FixRound_t` / `FixSaturate_t` 枚举值存成 7+4 个**字符串常量**，成为跨语言单一拼写真相源。
- 接口上的 `string` 泛型在实体内经 `cl_fix_format_from_string` / `cl_fix_round_from_string` 还原成类型常量，端口位宽则由 `fixFmtWidthFromString` 在 elaboration 期推导（\( W=S+I+F \)）。
- 舍入/饱和/寄存器三组泛型都有「按实体语义量身定做」的默认值；寄存器三态 `"YES"`/`"NO"`/`"AUTO"` 由 `fixImplementReg` 判定，`AUTO` = 「有逻辑才插」。
- 实例化时有四种等价写法：裸字符串、包常量、`to_string` 桥接（VHDL）、Verilog `localparam string`；相邻实体的格式与模式必须匹配。

## 7. 下一步学习建议

- 下一讲 [u8-l3 基本定点运算：resize/add/mult/round/saturate 等](u8-l3-basic-fix-operations.md) 会逐个拆解这些基本运算实体——本讲你已经掌握了它们的「接口语言（字符串泛型）」与「寄存器约定」，接下来聚焦每种运算的格式变换与延迟。
- 若想了解 Python 单一真相源如何批量生成这些定点常量/格式，跳到 [u8-l4 Python 代码生成：olo_fix_pkg_writer](u8-l4-python-codegen-pkg-writer.md)。
- 若想看本讲提到的 `fixFileRead*` 文件读取函数如何在协仿真中派上用场，参见 [u8-l5 协同仿真：olo_fix_cosim 与 sim_stimuli/checker](u8-l5-cosimulation.md)。
- 建议本地 `git clone --recursive` 检出 `en_cl_fix` 子模块后，通读其 `en_cl_fix_pkg.vhd` 中 `cl_fix_format_from_string` / `cl_fix_round_from_string` / `cl_fix_width` 的实现，把本讲「黑盒借用」的这几个函数变成白盒。
