# u2-l3 MATLAB 实现模型与使用方式

## 1. 本讲目标

本讲聚焦 en_cl_fix 三语言实现中的 **MATLAB 一极**。前面 u2-l1 学了 Python 的包结构与测试，u2-l2 学了 VHDL 的 testbench 流程，本讲把第三块拼图补齐。

读完本讲你应该能够：

1. 理解 MATLAB 端「一个 `.m` 文件对应一个 `cl_fix_` 函数」的工程组织，以及靠文件头注释充当 `help` 文档的约定。
2. 掌握 **必须先执行 `cl_fix_constants` 脚本**才能建立 `Sat.*` / `Round.*` 常量结构这一关键约定，并理解每个函数内部为何又自行调用一次。
3. 读懂 `cl_fix_format` 如何用一个普通 struct 表示 `[S,I,F]` 三元组，以及其中被注释掉的「52 位」约束。
4. 对照 `cl_fix_from_real`，理解 MATLAB 与 VHDL **位真一致性的来源**——同样的 `round` 到无穷（半值远离零）量化。
5. 读懂 `cl_fix_resize` 的「加偏移再 `floor` 截断」实数实现，并能列出 MATLAB 相对 VHDL 缺失的函数（即 MATLAB 是功能子集）。

## 2. 前置知识

本讲假设你已经学完：

- **u1-l2** 定点格式 `[S,I,F]` 三元组与位宽 `W=S+I+F`。
- **u1-l3** `cl_fix_width` 等格式查询函数，以及 MATLAB 端 `max_value/min_value` 直接返回实数、且受 IEEE754 双精度 52 位尾数限制这一差异。
- **u1-l4** 七种舍入模式 `FixRound`，尤其是「对称舍入到无穷 `SymInf_s`（半值远离零）」。
- **u1-l5** 四种饱和模式 `FixSaturate`（None/Warn/Sat/SatWarn），以及 `cl_fix_from_real` 的默认饱和行为。

几个本讲会用到的 MATLAB 基础概念（不熟悉也没关系，下面会解释）：

- **脚本（script）与函数（function）**：MATLAB 里 `.m` 文件若以 `function` 关键字开头就是函数，有自己的独立工作区；否则就是脚本，脚本中的赋值会落到**调用者**的工作区里。`cl_fix_constants.m` 就是一个脚本。
- **struct（结构体）**：MATLAB 的轻量数据容器，用 `.` 访问字段，如 `fmt.Signed`。MATLAB 端的定点格式就是一个 struct。
- **`help` 命令**：在 MATLAB 命令行键入 `help 函数名`，会自动打印该 `.m` 文件**顶部紧跟函数名的注释块**——这是 MATLAB 特有的「注释即文档」机制。
- **向量化**：和 Python 端一样，MATLAB 的运算天然支持对数组/矩阵逐元素执行（如 `round([0.5, 1.5])`）。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|---|---|
| `matlab/src/cl_fix_constants.m` | **脚本**，建立 `Sat.*` 与 `Round.*` 常量结构，是所有 `cl_fix_` 函数的前提 |
| `matlab/src/cl_fix_format.m` | 构造定点格式 struct（Signed/IntBits/FracBits） |
| `matlab/src/cl_fix_from_real.m` | 把 double 转成定点数，含舍入与饱和；是与 VHDL 对照位真一致性的核心 |
| `matlab/src/cl_fix_resize.m` | 把定点数 resize 到另一格式，含七种舍入 + 四种饱和的实数实现 |
| `matlab/src/cl_fix_write_int.m` | 文件写入示例，体现 MATLAB 端如何复用 `cl_fix_from_real` |
| `vhdl/src/en_cl_fix_pkg.vhd` | VHDL 对照实现（重点看 `cl_fix_from_real`） |
| `README.md` | 说明 MATLAB 无测试、`help` 文档约定与运行方式 |

`matlab/src/` 目录共 **33 个 `.m` 文件**，每个文件名就是一个 `cl_fix_` 函数。

## 4. 核心概念与源码讲解

### 4.1 MATLAB 工程组织：一文件一函数、help 文档与无测试现状

#### 4.1.1 概念说明

en_cl_fix 的三种语言在「如何组织源码」上各不相同（见 u1-l1）：

- **VHDL**：把全部函数塞进**一个**大包文件 `en_cl_fix_pkg.vhd`（两千多行）。
- **Python**：按职责拆成 `en_cl_fix_types.py` / `en_cl_fix_pkg.py` / `wide_fxp.py` 三个模块，再用 `__init__.py` 统一导出（见 u2-l1）。
- **MATLAB**：采用 **「一个 `.m` 文件对应一个 `cl_fix_` 函数」** 的扁平组织——`cl_fix_add.m`、`cl_fix_resize.m`、`cl_fix_from_real.m` ……共 33 个文件平铺在 `matlab/src/` 下。

这种组织是 MATLAB 的语言惯例：MATLAB 要求**文件名必须与文件内的主函数名一致**，所以「一函数一文件」不是风格选择，而是 MATLAB 的硬性要求。好处是函数可被单独 `help`、单独查找；代价是文件数量多、没有统一的命名空间门面（Python 有 `__init__.py`，VHDL 有 `use work.en_cl_fix_pkg.all`，MATLAB 只能靠把目录加进 `path`）。

#### 4.1.2 核心流程

使用 MATLAB 实现的标准流程：

1. 把 `matlab/src/` 加入 MATLAB 搜索路径（`addpath`）。
2. 在命令行（或脚本开头）执行一次 `cl_fix_constants`，建立 `Sat` / `Round` 常量。
3. 用 `cl_fix_format(S,I,F)` 构造格式 struct。
4. 调用 `cl_fix_from_real` / `cl_fix_resize` / `cl_fix_add` 等函数做运算。
5. 想查某函数用法时，键入 `help cl_fix_xxx`，MATLAB 自动打印该文件头注释。

README 把这一约定写得很清楚——MATLAB 的文档机制是「注释即 `help` 输出」，前提是路径已设置：

- [README.md:L130-L132](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/README.md#L130-L132)（MATLAB 文档注释，`help <command>` 自动显示，需先把 `.m` 目录加进 path）

#### 4.1.3 源码精读

每个 `.m` 文件的开头都有一段格式整齐的注释块，它既是给人读的说明，也是 `help` 命令的输出。以 `cl_fix_from_real.m` 为例：

- [matlab/src/cl_fix_from_real.m:L6-L13](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_from_real.m#L6-L13)：文档注释，写明函数用途、调用签名 `RESULT = cl_fix_from_real(A, RESULT_FMT, SATURATE)` 与每个参数的含义。

注意第 15 行这句前提提示：

- [matlab/src/cl_fix_from_real.m:L15](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_from_real.m#L15)（`The script cl_fix_constants must be executed prior to this function.`）

这条注释会原样出现在 `help cl_fix_from_real` 的输出里，等于把「必须先跑 constants」这一前置约束直接暴露给使用者。

另一个重要的工程现状：**MATLAB 实现目前没有任何测试**。Python 有 `en_cl_fix_pkg_test.py`（u2-l1），VHDL 有 testbench + `sim.tcl`（u2-l2），而 README 明说 MATLAB 没有：

- [README.md:L38-L39](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/README.md#L38-L39)（MATLAB 一节，仅有一句「Currently there are not tests for the MATLAB implementation」）

这也是为什么本讲的代码实践多为「源码阅读 + 手算验证」型，而非「跑测试看绿灯」型。

#### 4.1.4 代码实践

1. **实践目标**：验证 MATLAB「注释即 `help` 文档」机制，并理解一文件一函数组织。
2. **操作步骤**：
   - 打开 [matlab/src/cl_fix_from_real.m](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_from_real.m)，阅读第 1–23 行的注释块。
   - 数一数 `matlab/src/` 下的 `.m` 文件个数，确认文件名与其内部主函数名一一对应。
3. **需要观察的现象**：若有 MATLAB 环境，执行 `addpath('matlab/src')` 后键入 `help cl_fix_from_real`，应当看到第 6–23 行注释作为帮助文本打印出来。
4. **预期结果**：注释块中声明的调用签名 `RESULT = cl_fix_from_real(A, RESULT_FMT, SATURATE)` 与第 25 行真实函数定义 `function result = cl_fix_from_real (a, result_fmt, saturate)` 完全对应；文件总数为 33。
5. `help` 的实际输出格式属 MATLAB 运行时行为，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 MATLAB 不能像 Python 那样用一个 `__init__.py` 把所有函数统一导出？
**答案**：因为 MATLAB 的语言约定是「文件名 = 主函数名」，每个公开函数必须独占一个同名 `.m` 文件；MATLAB 没有 Python 那种「一个模块里放多个顶层函数再统一 import」的机制。访问它们的方式是把目录加进 `path`。

**练习 2**：在 `matlab/src/` 下能否找到类似 VHDL `en_cl_fix_pkg_tb.vhd` 的 testbench 文件？
**答案**：不能。README 第 39 行明确说明 MATLAB 实现目前没有测试，`matlab/` 目录下也只有 `src/`，没有 `unittest/` 或 `tb/`。

---

### 4.2 cl_fix_constants：Sat/Round 常量结构与初始化约定

#### 4.2.1 概念说明

回顾 u1-l4、u1-l5：舍入模式 `FixRound` 与饱和模式 `FixSaturate` 在三种语言中共享**同一套整数编码**（舍入 0–6、饱和 0–3），这是位真一致性的前提。

- VHDL 用枚举类型 `FixRound_t` / `FixSaturate_t`。
- Python 用 `Enum`（`FixRound.Trunc_s` 等）。
- **MATLAB 没有真正的枚举类型**，于是用一个名为 `cl_fix_constants` 的**脚本**，往工作区里写入两个 struct：`Sat` 和 `Round`，每个模式是一个整数字段。使用者用 `Sat.SatWarn_s`、`Round.Trunc_s` 这样的写法引用，读起来和 VHDL/Python 几乎一样。

#### 4.2.2 核心流程

`cl_fix_constants` 的执行流程极简——它就是一串赋值语句：

1. 建立 `Sat` struct，写入 4 个饱和模式字段（0–3）。
2. 建立 `Round` struct，写入 7 个舍入模式字段（0–6）。

关键在于「它是个脚本」：脚本没有独立工作区，被谁调用，变量就落到谁的工作区。因此：

- 在**命令行**手动执行一次 `cl_fix_constants`，`Sat`/`Round` 进入 base workspace，后续交互调用 `cl_fix_from_real` 时就能用到它们（如果函数内部不自行初始化的话）。
- 在**函数内部**（如 `cl_fix_from_real` 第 27 行）调用 `cl_fix_constants`，则 `Sat`/`Round` 进入该函数的局部工作区——这是一种「自我初始化」的防御式写法，保证函数不依赖外部是否跑过 constants。

整数编码对照表（位真一致性的根基）：

| 模式 | MATLAB 字段 | 整数值 |
|---|---|---|
| 饱和-不处理 | `Sat.None_s` | 0 |
| 饱和-仅告警 | `Sat.Warn_s` | 1 |
| 饱和-夹紧 | `Sat.Sat_s` | 2 |
| 饱和-夹紧并告警 | `Sat.SatWarn_s` | 3 |
| 舍入-截断 | `Round.Trunc_s` | 0 |
| 舍入-非对称正 | `Round.NonSymPos_s` | 1 |
| 舍入-非对称负 | `Round.NonSymNeg_s` | 2 |
| 舍入-对称到无穷 | `Round.SymInf_s` | 3 |
| 舍入-对称到零 | `Round.SymZero_s` | 4 |
| 舍入-收敛到偶 | `Round.ConvEven_s` | 5 |
| 舍入-收敛到奇 | `Round.ConvOdd_s` | 6 |

#### 4.2.3 源码精读

- [matlab/src/cl_fix_constants.m:L6-L8](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_constants.m#L6-L8)：注释说明「本脚本必须在任何 `cl_fix_` 函数之前执行」。注意文件里**没有 `function` 关键字**，所以它是脚本。
- [matlab/src/cl_fix_constants.m:L10-L13](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_constants.m#L10-L13)：写入 `Sat` 的 4 个字段，整数 0–3。
- [matlab/src/cl_fix_constants.m:L16-L22](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_constants.m#L16-L22)：写入 `Round` 的 7 个字段，整数 0–6。

而每个使用这些常量的函数，都会在函数体第一行再次调用它，例如：

- [matlab/src/cl_fix_from_real.m:L27](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_from_real.m#L27)：`cl_fix_from_real` 内部首行 `cl_fix_constants`，自建局部 `Sat`/`Round`。
- [matlab/src/cl_fix_resize.m:L38](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_resize.m#L38)：`cl_fix_resize` 同样如此。

#### 4.2.4 代码实践

1. **实践目标**：确认 MATLAB 常量编码与 VHDL/Python 完全一致。
2. **操作步骤**：在 MATLAB 中执行 `cl_fix_constants`，然后依次打印 `Sat.SatWarn_s`、`Sat.None_s`、`Round.Trunc_s`、`Round.SymInf_s`。
3. **需要观察的现象**：四个值分别为 3、0、0、3。
4. **预期结果**：与上表一致，也与 u1-l4/u1-l5 中 VHDL 枚举的整数序号一致。
5. 若无 MATLAB 环境，可直接阅读上面两条永久链接的源码核对数值，**不必依赖运行**。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `cl_fix_constants` 改写成一个带 `function` 关键字的函数，会发生什么？
**答案**：函数有独立工作区，`Sat`/`Round` 不会落到调用者那里；调用 `cl_fix_constants` 后命令行里依然看不到 `Sat`，后续 `cl_fix_from_real` 内部那句 `cl_fix_constants` 也无法再像现在这样「顺便初始化局部常量」（因为作为函数调用它不会有副作用外泄，但函数内部的赋值仍局限于自身——只是 base workspace 的 `Sat`/`Round` 消失）。这正是作者把它写成**脚本**的原因。

**练习 2**：`Round.NonSymPos_s` 在 VHDL 里常被起别名叫什么？（回顾 u1-l4）
**答案**：`Round_s`。它是「最常用」的非对称正舍入的简写别名。

---

### 4.3 cl_fix_format：定点格式构造与 52 位约束

#### 4.3.1 概念说明

`cl_fix_format` 是 MATLAB 端构造定点格式的入口。回顾 u1-l2：格式三元组 `[S,I,F]` 在 VHDL 里是 `record FixFormat_t`、在 Python 里是 `FixFormat` 类，在 **MATLAB 里就是一个普通 struct**，含三个字段 `Signed`、`IntBits`、`FracBits`。它只有数据、没有方法（不像 Python 的 `FixFormat.width()`）——位宽计算交给独立的 `cl_fix_width.m` 函数完成。

回顾 u1-l3：MATLAB 端受 IEEE754 双精度尾数限制，格式位宽有个「理论上限 52」。在 `cl_fix_format` 里能看到这个上限检查的**痕迹**——但它被注释掉了。

#### 4.3.2 核心流程

`cl_fix_format(signed, intBits, fracBits)` 做三件事：

1. 检查硬约束 `intBits + fracBits >= 1`（与 u1-l2 一致），违例则 `error`。
2. （被注释掉的）检查 `intBits + fracBits <= 52`。
3. 组装并返回 struct：`Signed = signed > 0`、`IntBits = intBits`、`FracBits = fracBits`。

注意第 1 步用 `error`（MATLAB 抛异常的方式），与 VHDL 的 `assert severity failure`、Python 的「不检查」形成对比——这是 u1-l2/u1-l3 已讨论的跨语言差异。

#### 4.3.3 源码精读

- [matlab/src/cl_fix_format.m:L8-L13](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_format.m#L8-L13)：文档注释，说明用法 `FORMAT = cl_fix_format(SIGNED, INTBITS, FRACBITS)`。
- [matlab/src/cl_fix_format.m:L17-L19](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_format.m#L17-L19)：硬约束检查，`(intBits+fracBits) < 1` 即 `error`。
- [matlab/src/cl_fix_format.m:L20-L22](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_format.m#L20-L22)：被注释掉的 52 位上限检查——`error` 语句整段被 `%` 注释，所以**当前实际不生效**。
- [matlab/src/cl_fix_format.m:L24-L26](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_format.m#L24-L26)：组装 struct。注意 `fmt.Signed = signed > 0`，把任意「大于 0」的入参归一化为逻辑真。

被注释掉的 52 位检查是个值得留意的细节：它说明作者知道双精度的精度上限（u1-l3），但选择**不在构造期强制**——大概是为了不阻止用户构造「理论格式」做格式运算（哪怕数值装不下），而把真正的精度限制留给运行时的浮点误差。

#### 4.3.4 代码实践

1. **实践目标**：验证格式构造与硬约束。
2. **操作步骤**：
   - `fmt = cl_fix_format(true, 3, 2)`，然后查看 `fmt.Signed / fmt.IntBits / fmt.FracBits`。
   - 尝试 `cl_fix_format(true, 0, 0)`，观察是否抛错。
   - 尝试 `cl_fix_format(true, 60, 0)`（超过 52 位），观察是否抛错。
3. **需要观察的现象**：第一个返回 `Signed=true, IntBits=3, FracBits=2` 的 struct；第二个因 `0+0<1` 抛出 `error`；第三个**不抛错**（52 位检查被注释）。
4. **预期结果**：与源码第 17–22 行的逻辑一致。
5. 第三个用例的运行结果依赖当前 MATLAB 源码版本（注释状态），**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：MATLAB 的 `fmt` struct 上有没有像 Python `FixFormat.width()` 那样的方法？位宽怎么算？
**答案**：没有方法，struct 只存数据。位宽由独立的 `cl_fix_width(fmt)` 函数计算，公式 `S+I+F`（见 u1-l3）。

**练习 2**：为什么 `cl_fix_format(true, 60, 0)` 在 MATLAB 端不报错，但实际存数会有问题？
**答案**：52 位上限检查在源码里被注释掉了，所以构造期放行；但 IEEE754 双精度只有 53 位有效整数精度，60 位整数无法精确表示，后续 `cl_fix_from_real` 装数会出现精度损失（这正是 u1-l3 提到的 MATLAB 限制，也是 Python 端引入 `wide_fxp` 大位宽实现的原因，见 Unit 6）。

---

### 4.4 cl_fix_from_real：与 VHDL 位真一致性的来源

#### 4.4.1 概念说明

`cl_fix_from_real` 把一个 double（实数）量化装入定点格式。它是 MATLAB 与 VHDL **位真一致性**的关键对照点——本讲的核心问题就是：「凭什么 MATLAB 算出来的定点值和 VHDL 综合后的硬件逐位相同？」

答案藏在**量化那一步**：MATLAB 和 VHDL 都用 `round()`（半值远离零 = 对称舍入到无穷 `SymInf_s`）来量化。只要量化方式相同，对同一输入、同一格式，结果必然逐位一致。MATLAB 源码注释直接点破了这一点。

需要区分两个层面（回顾 u1-l5）：
- **量化**：用 `round` 把实数对齐到格式网格。这一步在 `from_real` 里**固定**用 round-to-infinity，**不受** `FixRound` 参数控制。
- **饱和**：处理越界值。这一步**受** `saturate` 参数控制。

#### 4.4.2 核心流程

`cl_fix_from_real(a, result_fmt, saturate)` 的执行流程：

1. 第 27 行：调用 `cl_fix_constants` 自建局部常量。
2. 第 30 行：**量化**——`result = round(a .* 2^F) .* 2^(-F)`，其中 `F = result_fmt.FracBits`。
3. 第 32–43 行：若 `saturate` 为 `Warn_s`/`SatWarn_s`，检查是否越界并 `warning`。
4. 第 46–63 行：按 `saturate` 模式收尾——`None_s`/`Warn_s` 做**回绕**（`mod`），`Sat_s`/`SatWarn_s` 做**夹紧**（clip 到上下界）。

量化公式（核心）：

\[ V_{\text{quantized}} = \mathrm{round}\!\left(a \cdot 2^{F}\right) \cdot 2^{-F} \]

`round` 为「半值远离零」：`round(0.5)=1`、`round(-0.5)=-1`、`round(2.5)=3`、`round(-2.5)=-3`，即对称舍入到无穷 `SymInf_s`。

#### 4.4.3 源码精读

- [matlab/src/cl_fix_from_real.m:L29-L30](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_from_real.m#L29-L30)：量化核心。注释 `% round symmetrically to infinity (same as VHDL)` 由作者亲口确认「与 VHDL 相同」。
- 对照 VHDL 实现 [vhdl/src/en_cl_fix_pkg.vhd:L1671](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1671)：`ASat_v := round(ASat_v * 2.0**(result_fmt.FracBits));`——同样是 `round`。这就是位真一致性的**来源**：两语言用同一个 `round` 做量化。
- [matlab/src/cl_fix_from_real.m:L46-L63](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_from_real.m#L46-L63)：`switch saturate` 分支——MATLAB **会**根据 `saturate` 模式决定回绕还是夹紧。

**与 VHDL 的一个重要差异**（u1-l5 已提及，这里落到源码）：VHDL 的 `cl_fix_from_real` 在量化前**无条件**把输入夹到 `[min_real, max_real]`，且**完全不理会** `saturate` 参数：

- [vhdl/src/en_cl_fix_pkg.vhd:L1661-L1668](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1661-L1668)：`if a > max_real ... elsif a < min_real ...`，永远夹紧，不看 `saturate`。

而 MATLAB 的 `cl_fix_from_real`（第 46–63 行）严格按 `saturate` 分支：传 `None_s` 会回绕而非夹紧。因此：

- **对落在范围内的输入**（绝大多数仿真场景）：两者都走 round-to-infinity 量化，结果**逐位相同**——这就是位真。
- **对越界输入**：若 MATLAB 侧传默认的 `SatWarn_s`，它也会夹紧，仍与 VHDL 一致；若传 `None_s`，则 MATLAB 回绕、VHDL 夹紧，**不一致**。所以跨语言对比时，MATLAB 侧应使用 `SatWarn_s`（VHDL 的默认值）以保持位真。

#### 4.4.4 代码实践

1. **实践目标**：亲手验证「round 到无穷」是位真一致性的来源。
2. **操作步骤**：
   - 在 MATLAB 中执行 `round([-2.5, -0.5, 0.5, 2.5])`，观察四个结果。
   - 用 `cl_fix_format(true,3,2)` 建格式，调用 `cl_fix_from_real(-3.25, fmt, Sat.SatWarn_s)`。
3. **需要观察的现象**：`round` 给出 `[-3, -1, 1, 3]`（半值远离零）；`-3.25` 装入 `(true,3,2)`（范围 -4…3.75）落在范围内，结果就是 `-3.25` 本身。
4. **预期结果**：`-3.25 × 2² = -13`，`round(-13) = -13`，`-13 × 2⁻² = -3.25`，无饱和。验证了量化公式。
5. `round` 的实际输出属 MATLAB 运行时行为，**待本地验证**（但依据 IEEE754 与 MATLAB 文档可推断为远离零）。

#### 4.4.5 小练习与答案

**练习 1**：把 `-0.5` 装入 `(true,0,0)`（即整数格式），MATLAB `cl_fix_from_real` 会得到什么？这与 `Trunc_s` 一致吗？
**答案**：`round(-0.5 × 1) × 1 = round(-0.5) = -1`（远离零）。`Trunc_s` 会得到 0（向负取整后的截断等价于朝 0 砍，`-0.5` 截断为 0）。所以**不一致**——`from_real` 固定用 round-to-infinity，不受 `Trunc_s` 影响。

**练习 2**：为什么说「跨语言位真对比时，MATLAB 侧应传 `SatWarn_s`」？
**答案**：因为 VHDL 的 `cl_fix_from_real` 无条件夹紧（对应 `SatWarn_s`/`Sat_s` 行为）。MATLAB 侧若传 `None_s` 会回绕，对越界输入就和 VHDL 不一致；传 `SatWarn_s` 才与 VHDL 的默认行为对齐。

---

### 4.5 cl_fix_resize 的实数实现与 MATLAB 功能子集

#### 4.5.1 概念说明

`cl_fix_resize` 是整个库的心脏（u3-l2/u3-l3 会深入）。本讲只看 **MATLAB 版本的实现风格**：和 `cl_fix_from_real` 一样，它操作的 `a` 是 **double 实数**（不是 VHDL 的 `std_logic_vector` 位串），因此舍入与饱和都用**浮点算术**实现——「加一个偏移再 `floor` 截断」来模拟硬件的「加偏移再砍位」。

同时，本模块收尾给出 MATLAB 相对 VHDL **缺失的函数清单**，回答「MATLAB 是功能子集」这一学习目标。

#### 4.5.2 核心流程

`cl_fix_resize(a, a_fmt, result_fmt, round, saturate)` 流程：

1. 第 38 行：自建常量。
2. 第 40–59 行：**舍入**——仅当 `round ≠ Trunc_s` 且 `a_fmt.FracBits > result_fmt.FracBits`（即要丢小数位）时，给 `a` 加一个模式相关的偏移。`Trunc_s` 不加偏移（直接截断）。
3. 第 62 行：**截断**——`result = floor(a .* 2^resultFrac) .* 2^(-resultFrac)`，用 `floor` 砍掉多余小数位。
4. 第 64–95 行：**饱和告警 + 回绕/夹紧**，与 `cl_fix_from_real` 完全同构。

七种舍入对应的偏移（节选）：

| 模式 | 偏移（加到 `a` 上） |
|---|---|
| `Trunc_s` | 无 |
| `NonSymPos_s` | \(+2^{-F_{\text{result}}-1}\)（半个结果 LSB） |
| `NonSymNeg_s` | \(+2^{-F_{\text{result}}-1} - 2^{-F_a}\) |
| `SymInf_s` | \(+2^{-F_{\text{result}}-1} - 2^{-F_a}\cdot[a<0]\) |
| `SymZero_s` | \(+2^{-F_{\text{result}}-1} - 2^{-F_a}\cdot[a\ge 0]\) |

其中 \(F_a = a\_fmt.FracBits\)。这正是 u1-l4 所述「加偏移再截断」机制在浮点上的直接表达。

#### 4.5.3 源码精读

- [matlab/src/cl_fix_resize.m:L1-L35](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_resize.m#L1-L35)：文件头注释（即 `help cl_fix_resize` 输出），列出全部 7 种 `Round.*` 与 4 种 `Sat.*` 取值——这是「`cl_fix_resize` 注释」这一最小模块的载体。
- [matlab/src/cl_fix_resize.m:L40-L59](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_resize.m#L40-L59)：七种舍入的偏移 `switch`。第 41 行的条件 `round && a_fmt.FracBits > result_fmt.FracBits` 注意一个细节：`round` 本身是整数（模式编号），非 `Trunc_s`(0) 即为真，巧妙复用了「0=false」。
- [matlab/src/cl_fix_resize.m:L62](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_resize.m#L62)：`floor` 截断。
- [matlab/src/cl_fix_resize.m:L64-L95](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_resize.m#L64-L95)：饱和告警 + 回绕/夹紧，与 `cl_fix_from_real` 第 32–63 行几乎逐行相同——MATLAB 端靠**复制粘贴**这段饱和逻辑实现多个函数（VHDL 端则统一落到 `cl_fix_resize` 一个出口，见 u7-l1）。

**MATLAB 功能子集对照**：把 `matlab/src/` 的 33 个函数与 VHDL 的 56 个 `cl_fix_*` 函数对比，MATLAB 缺失以下类别：

| 类别 | VHDL 有、MATLAB 无 | 说明 |
|---|---|---|
| 字符串解析 | `cl_fix_format_from_string` | MATLAB 仅有反向的 `cl_fix_string_from_format` |
| 实数边界 | `cl_fix_max_real`、`cl_fix_min_real` | MATLAB 的 `max_value/min_value` 已返回实数，冗余 |
| 整数转换 | `cl_fix_from_int`、`cl_fix_to_int` | MATLAB 用原生 double 即可 |
| 实数反查 | `cl_fix_to_real` | MATLAB 数值本身就是实数 |
| 二进制串 | `cl_fix_from_bin`、`cl_fix_to_bin` | — |
| 十六进制串 | `cl_fix_from_hex`、`cl_fix_to_hex` | — |
| 位整数 | `cl_fix_get_bits_as_int`、`cl_fix_from_bits_as_int` | — |
| 文件读取 | `cl_fix_read_int/real/bin/hex` | MATLAB 仅 `write_int/write_real`，无 read、无 bin/hex |
| 取整辅助 | `cl_fix_floor`、`cl_fix_ceil`、`cl_fix_round` | MATLAB 用原生 `floor/ceil/round` |
| 比较 | `cl_fix_compare` | — |
| 仿真辅助 | `cl_fix_random`、`cl_fix_write_formats` | 这两个其实是 **Python 独有**，VHDL 也没有 |

可见 MATLAB 实现是**功能子集**：它聚焦「建格式 → 装数（from_real）→ 运算（add/sub/mult/resize…）→ 写文件（write_int/real）」这条算法评估主链路，省去了 VHDL/Python 为硬件协同仿真与跨语言数据交换而准备的大量「与位串/字符串/文件互转」的胶水函数。

#### 4.5.4 代码实践

1. **实践目标**：体验 MATLAB resize 的「加偏移再 floor」，并验证子集结论。
2. **操作步骤**：
   - 建 `aFmt = cl_fix_format(true,3,4)`、`rFmt = cl_fix_format(true,3,1)`，取一个值如 `a = 1.6875`。
   - 调用 `cl_fix_resize(a, aFmt, rFmt, Round.NonSymPos_s, Sat.SatWarn_s)`。
   - 手算：`result_frac=1`，加偏移 `2^(-1-1)=0.25`，得 `1.9375`，`floor(1.9375 × 2^1) × 2^-1 = floor(3.875) × 0.5 = 3 × 0.5 = 1.5`。
3. **需要观察的现象**：MATLAB 输出应为 `1.5`，与手算一致。
4. **预期结果**：`1.5`，落在 `(true,3,1)` 范围 -4…3.5 内，无饱和。
5. 数值运行结果**待本地验证**（依据源码逻辑推断为 1.5）。

#### 4.5.5 小练习与答案

**练习 1**：MATLAB 的 `cl_fix_resize` 第 41 行 `if round && ...`，为什么 `round` 能直接当布尔用？
**答案**：因为 `round` 变量存的是舍入模式整数编号，`Trunc_s = 0`。MATLAB 中 `0` 为假、非零为真，所以「`round` 非 0」等价于「不是截断模式」，巧妙复用了编码。

**练习 2**：MATLAB 缺 `cl_fix_to_real`，那怎么从一个定点「数」看到它的实数值？
**答案**：MATLAB 端的定点数**本身就是一个 double 实数**（不是位串），所以无需 `to_real`——直接就是实数。这也是 MATLAB 能省下一大批转换函数的根本原因。

**练习 3**：相比 VHDL，MATLAB 多了什么、少了什么？（一句话）
**答案**：少了所有「位串/字符串/文件/整数互转」与 `compare` 等胶水函数；本质上没有「多」——它是最精简的算法评估子集（`random`/`write_formats` 那两个仿真辅助是 Python 独有，并非 MATLAB 多出）。

---

## 5. 综合实践

把本讲的格式构造、constants 约定、from_real 位真、功能子集四件事串起来：

1. **实践目标**：用 MATLAB 走完一次「建常量 → 建格式 → 装数 → 对照 VHDL → 盘点缺失」的完整链路。
2. **操作步骤**：
   1. `addpath('matlab/src'); cl_fix_constants;`（建立 `Sat`/`Round`）。
   2. `fmt = cl_fix_format(true, 3, 2);`（范围 -4…3.75）。
   3. `r = cl_fix_from_real(-3.25, fmt, Sat.SatWarn_s);`（装入 -3.25）。
   4. 手算验证：`-3.25 × 2² = -13`，`round(-13) = -13`，`-13 × 2⁻² = -3.25`，落在范围内，结果 `-3.25`。
   5. 打开 VHDL 对照 [vhdl/src/en_cl_fix_pkg.vhd:L1651-L1682](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1651-L1682)，确认两者量化都用 `round`（第 1671 行），故结果一致。
   6. 列出 `matlab/src/` 相比 VHDL 缺失的函数：`to_real`、`from_int`、`to_int`、`from_bin`、`to_bin`、`from_hex`、`to_hex`、`format_from_string`、`max_real`、`min_real`、`get_bits_as_int`、`from_bits_as_int`、`read_int`、`read_real`、`read_bin`、`read_hex`、`write_bin`、`write_hex`、`floor`、`ceil`、`round`、`compare`。
3. **需要观察的现象**：第 3 步得到 `-3.25`；第 5 步看到 VHDL 与 MATLAB 量化表达式同构；第 6 步清单与 4.5.3 表格吻合。
4. **预期结果**：MATLAB 的 `-3.25` 与 VHDL 对同一输入量化的结果逐位相同（位真）；缺失函数清单证明 MATLAB 是功能子集。
5. 数值与 `help` 输出属 MATLAB 运行时行为，**待本地验证**；源码对照部分可直接依据本讲永久链接核对。

## 6. 本讲小结

- MATLAB 采用「一个 `.m` 文件对应一个 `cl_fix_` 函数」的扁平组织，文件头注释即 `help` 文档；目前**没有测试**。
- `cl_fix_constants` 是**脚本**（非函数），建立 `Sat`/`Round` 两个 struct，整数编码 0–3 与 0–6，与 VHDL/Python 共享——这是位真一致性的编码前提。
- 每个函数内部还会再调用一次 `cl_fix_constants`，靠「脚本共享调用者工作区」实现自我初始化。
- `cl_fix_format` 返回普通 struct（仅数据、无方法），硬约束 `I+F≥1` 用 `error`，而 52 位上限检查**被注释掉、当前不生效**。
- MATLAB 与 VHDL 位真一致性的来源是**同一个 `round`（半值远离零 = SymInf_s）量化**；但 `from_real` 的饱和处理两语言不同（MATLAB 按 `saturate` 分支，VHDL 无条件夹紧），跨语言对比应统一用 `SatWarn_s`。
- `cl_fix_resize` 用浮点「加偏移再 `floor`」实现七种舍入；MATLAB 是功能子集，缺少 `to_real/from_int/to_int/from_hex/to_hex/compare/read_*/write_bin/write_hex` 等一批转换与文件函数。

## 7. 下一步学习建议

- 想看 MATLAB 没有但 VHDL/Python 有的转换函数怎么工作，进入 **u3-l1 数值与字符串转换函数**。
- 想深入 `cl_fix_resize` 的舍入/饱和硬件实现（含 `TempFmt`、`HalfMinusDelta`、`CutIntSignBits`），进入 **u3-l2 / u3-l3**。
- 想理解 MATLAB 缺失的 `cl_fix_compare`、`mean_angle` 等精巧算法，进入 **u7-l2**。
- 想了解 MATLAB 52 位上限之外的「大位宽」怎么办，进入 **Unit 6 的 Python `wide_fxp`**。
- 想看 MATLAB 仅有的两个文件写函数如何串起跨语言位真数据交换，进入 **u5-l1 文件读写与位真数据交换**。
