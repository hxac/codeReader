# Python 代码生成器

## 1. 本讲目标

上一讲（u8-l1）我们读完了「函数无关」的线性近似内核 `psi_fix_lin_approx_calc`：它不含任何表，靠 `addr_table_o` 出地址、`data_table_i` 回数据，与外部 ROM 解耦。那么这些 ROM 里的 offset/gradient 数值是从哪里来的？本讲就回答这个问题——**psi_fix 用一个 Python 脚本，从一段数学函数出发，一次性算出定点表、生成 `.vhd` 组件实体、生成自检测试台与比对数据**。

学完本讲你应当能够：

- 说清 `model/psi_fix_lin_approx.py` 如何对一个数学函数采样、求数值导数、量化出 offset 表与 gradient 表；
- 解释 `GenerateEntity`/`GenerateTb` 如何用「占位符替换」把 `snippets/*.vhd` 模板填成具体组件；
- 把 `sin18b / sqrt18b / inv18b / gaussify20b` 四个组件逐一对应到 `CONFIGS` 里的某一项配置；
- 理解「手写内核 + 自动生成表 + 自动生成测试」这套生成式组件的维护方式，知道改精度时该动哪个文件。

## 2. 前置知识

本讲默认你已经掌握：

- **位真双模型**（u1-l1、u3-l1）：每个 VHDL 组件必须有一个逐位一致的 Python 黄金参考，测试台逐位比对，不一致就报 `###ERROR###`。
- **定点格式 `[s,i,f]` 与位增长**（u1-l4、u2-l2）：总位宽 `W=s+i+f`，乘法整数位相加再 +1。本讲会反复用它推导表项格式。
- **`psi_fix_get_bits_as_int`**（u2-l1、u3-l2）：把一个定点值转成「其二进制位模式按有符号整数解读」的整数，是逐位比对与表项写入的底座。
- **lin_approx_calc 的表接口**（u8-l1）：内核把 `data_table_i` 这条总线拆成「gradient 高位 | offset 低位」两段。本讲生成的表必须严格按这个拼接顺序排列，否则位真会被破坏。

补充两个 Python/数学背景：

- **scipy 的 `derivative`**：用有限差分法对函数求数值导数。本讲用它算每段中心的梯度 \(f'(x_k)\)，无需手写解析导数。
- **「模板 + 占位符替换」代码生成**：读一个含 `<PLACEHOLDER>` 的文本骨架，用 `str.replace` 把占位符换成具体值，再写盘。这是最朴素、也最易审查的代码生成方式。

## 3. 本讲源码地图

本讲围绕「生成」一侧的四个文件（内核 `calc` 属于 u8-l1，本讲只把它当被例化的黑盒）：

| 文件 | 角色 | 关键内容 |
| --- | --- | --- |
| `model/psi_fix_lin_approx.py` | 位真模型 + 代码生成器（二合一） | 配置类 `psi_fix_lin_cfg_settings`、标准配置 `CONFIGS`、表采样/求导/量化、`GenerateEntity`/`GenerateTb` |
| `model/snippets/psi_fix_lin_approx_tmpl.vhd` | 组件实体模板 | 含 `<ENTITY_NAME>`、`<TABLE_CONTENT>` 等占位符的骨架，例化 `calc` 内核并读 ROM |
| `model/snippets/psi_fix_lin_approx_tb_tmpl.vhd` | 测试台模板 | 含 `ApplyTextfileContent`/`CheckTextfileContent` 的自检测试台骨架 |
| `hdl/psi_fix_lin_approx_sin18b.vhd` | **生成产物**（样例） | 由 `CONFIGS.Sin18Bit` 生成，含 2048 项定点 ROM |
| `model/psi_fix_lut.py` + `model/snippets/psi_fix_lut_tmpl.vhd` | 平行的 LUT 生成器 | 同样的「表→模板→VHDL」套路，但用于整表查表（非分段线性） |

阅读建议：先看 `psi_fix_lin_approx.py` 的 `CONFIGS`（知道有哪些函数要生成），再看构造函数（知道表怎么算），最后看 `GenerateEntity`/`GenerateTb`（知道怎么落盘）；拿 `psi_fix_lin_approx_sin18b.vhd` 当生成结果对照验证。

## 4. 核心概念与源码讲解

### 4.1 表生成算法

#### 4.1.1 概念说明

u8-l1 讲过，分段线性近似把输入区间等分成 \(P\) 段，每段存一对参数：段中心的函数值 **offset** \(o_k=f(c_k)\) 与段中心的导数 **gradient** \(g_k=f'(c_k)\)。运行时只算

\[
\hat f(x)=o_k+g_k\cdot (x-c_k)
\]

这里的「表」就是全部 \(P\) 对 \((o_k, g_k)\)。本模块要解决的问题是：**给定一个 Python 数学函数 `f(x)`，怎样自动算出这 \(P\) 对定点参数？**

psi_fix 的做法非常直接：

1. 在输入格式的可表示范围上等分 \(P\) 段，取每段中心 \(c_k\) 作为采样点；
2. offset 直接代值 \(o_k=f(c_k)\)；
3. gradient 用 scipy 的有限差分求导 \(g_k=f'(c_k)\)（不必手写解析导数）；
4. 用 `psi_fix_from_real(..., err_sat=False)` 把两列浮点值量化到各自的定点格式，得到两张整数表。

注意第 4 步的 `err_sat=False`：量化时不做饱和，超界的值直接回绕——这是故意的，因为如果设计阶段配置选得合理，表项本就不该超界；若超界说明 `offsFmt/gradFmt` 选小了，应该回去调配置（见 4.1.4 的设计模式），而不是悄悄饱和掩盖问题。

#### 4.1.2 核心流程

表生成的算法骨架（伪代码）：

```
输入: cfg.function, cfg.inFmt, cfg.offsFmt, cfg.gradFmt, cfg.points (P)
1. indexBits = log2(P)                       # 表地址位数
2. 推导 remFmt / idxFmt / intFmt / addFmt     # 各级中间格式（见 u8-l1）
3. inputRange = [lower_bound(inFmt), 2^inFmt.i]
4. step = (inputRange[1] - inputRange[0]) / P
5. centers_k = lower + step/2 + k*step        # 每段中心, k=0..P-1
6. 若 inFmt 有符号: 把 centers 旋转半圈（负值挪到上半表）  # 对齐 calc 的地址映射
7. gradients = scipy.derivative(function, centers)
8. offsets   = function(centers)
9. gradTable = psi_fix_from_real(gradients, gradFmt, err_sat=False)
10.offsTable = psi_fix_from_real(offsets,   offsFmt, err_sat=False)
```

第 6 步的「旋转」是和 u8-l1 内核配套的关键：calc 内部对 reminder 做了「MSB 取反」把无符号段内偏移转成有符号，相应地，对有符号输入，表的物理地址顺序必须把负半轴挪到高地址，才能让地址→数值的映射自洽。对 `sin18b`（无符号输入 `[0,0,20]`）则无需旋转。

#### 4.1.3 源码精读

**配置容器**——所有可调参数都装在一个数据类里，这是「一个函数 = 一份配置」的抽象：

[model/psi_fix_lin_approx.py:20-52](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L20-L52) —— `psi_fix_lin_cfg_settings`：持有 `function`(lambda)、`inFmt/outFmt/offsFmt/gradFmt`(四个定点格式)、`points`(段数)、`name`(生成文件名后缀)、`validRange`(近似有效区间)。构造时把 `validRange` 夹到 `inFmt` 的可表示范围内。

**标准配置**——全库只维护 4 个函数，全部写死在这个 `CONFIGS` 内嵌类里：

[model/psi_fix_lin_approx.py:78-114](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L78-L114) —— `CONFIGS`：定义 `Sin18Bit / Sqrt18Bit / Gaussify20Bit / Invert18Bit` 四项，末尾 `all = [...]` 把它们汇总成列表，供 `__main__` 遍历。注意每个函数都用 lambda 缩放自变量，例如 `lambda x: np.sin(x * 2 * np.pi)` 把无符号输入 \([0,1)\) 映射到整整一个正弦周期；sin 还乘了 `(1-1/2**17)` 故意把 +1 压下去，避免有符号输出格式 \([1,0,17]\) 在峰值处饱和（回顾 u1-l4：有符号格式能表示 -1.0 但不能表示 +1.0）。

**构造函数——表的真正计算处**：

[model/psi_fix_lin_approx.py:152-192](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L152-L192) —— 构造函数。要点逐段对照：

- L162 `indexBits = np.log2(cfg.points)`：表地址宽度（sin18b 的 2048 段 → 11 位）。
- L165-170 推导四个中间格式 `remFmt/idxFmt/intFmt/addFmt`——这与 u8-l1 内核里的 `RemFmt/IdxFmt/IntFmt/AddFmt` 用同一组位宽算式，是「模型与 RTL 共用一套格式推导」的位真印章。
- L172-175 等分区间取每段中心 `centers`。
- L176-177 有符号输入时旋转 centers（前述第 6 步）。
- L178 `gradients = derivative(self.cfg.function, centers, dx=1e-6)`：scipy 有限差分求导。
- L179 `offsets = self.cfg.function(centers)`：直接代值。
- L191-192 用 `psi_fix_from_real(..., err_sat=False)` 量化出 `gradTable` 与 `offsTable`。

**位真执行**（与 RTL 逐拍对应，是测试台比对数据的来源）：

[model/psi_fix_lin_approx.py:197-216](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L197-L216) —— `Approximate`：把输入拆成表索引 `_GetTblIdx` 与 reminder（L205 减 `2**(remFmt.i-1)` 正是 u8-l1 的「MSB 取反」的 Python 等价做法），再 `mult(grad, rem)` → `add(offs, ...)` → `resize` 到 `outFmt`，级间用 `trunc/wrap`、末端用 `round/sat`，与内核完全一致。

**设计模式**——选配置用的交互式入口：

[model/psi_fix_lin_approx.py:132-148](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L132-L148) —— `Design`：以 `designMode=True` 实例化，打印表项的实际范围与所需存储位宽，并画误差曲线。新增/调参一个近似函数时，先用它确认 `offsFmt/gradFmt` 够用、误差可接受，再把定稿的配置写进 `CONFIGS`。文件末尾 L355-359 留了调用范例（默认注释掉）。

#### 4.1.4 代码实践

**实践目标**：用一个可手算的样例，验证「表生成 = 段中心代值 + 段中心求导 + 量化」三件事，并把它和已提交的 `sin18b` 表第一项对上。

**操作步骤**：

1. 打开 `model/psi_fix_lin_approx.py:79-86` 的 `Sin18Bit` 配置，确认 `function=lambda x: np.sin(x*2*np.pi)`、`points=2048`、`gradFmt=(1,3,8)`、`offsFmt=(1,0,19)`。
2. 手算第一段中心：\(c_0 = \text{step}/2 = (1/2048)/2 = 1/4096\)。
3. 手算 offset：\(o_0 = \sin(2\pi c_0) = \sin(2\pi/4096) \approx 0.001534\)。
4. 手算 gradient：\(g_0 = 2\pi\cos(2\pi c_0) \approx 2\pi \approx 6.2832\)。
5. 量化到整数（位模式）：offset 量化到 \([1,0,19]\) → \(0.001534 \times 2^{19} \approx 804\)；gradient 量化到 \([1,3,8]\) → \(6.2832 \times 2^{8} \approx 1608\)。
6. 打开生成产物 `hdl/psi_fix_lin_approx_sin18b.vhd:38`，看表第一项。

**需要观察的现象**：生成表第一行恰为

```
std_logic_vector(to_signed(1608, 12) & to_signed(804, 20)),
```

**预期结果**：`1608` 与手算的 gradient 整数一致，`804` 与手算的 offset 整数一致；高位 12 位放 gradient、低位 20 位放 offset，二者拼成 32 位 `TableWidth_c`（见 [hdl/psi_fix_lin_approx_sin18b.vhd:27-32](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_sin18b.vhd#L27-L32) 的 `GradFmt_c`/`OffsFmt_c`/`TableWidth_c`）。这就证明了表确实是「段中心代值 + 数值求导 + 量化」算出来的，且与 u8-l1 内核期望的「gradient 高位 | offset 低位」拼接顺序吻合。

> 说明：以上是源码阅读型手算验证，不依赖运行环境；若要本地复现完整流程，可在装好 NumPy/SciPy 的环境里取消 [model/psi_fix_lin_approx.py:358](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L358) 的注释、运行该脚本观察 `Design` 打印的表范围与误差曲线（具体数值待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么表量化用 `err_sat=False`，而末端 `resize` 到 `outFmt` 却用 `round/sat`？

**答案**：表项是设计阶段的「半成品」，若它超界说明 `offsFmt/gradFmt` 选小了，应该回去改配置（用 `Design` 重新评估），用回绕暴露问题而非用饱和掩盖；末端 `resize` 是运行时把全精度中间结果收敛到用户输出格式，必须 `round/sat` 保证输出精度与不溢出。

**练习 2**：`sin18b` 的 `function` 里为什么要乘 `(1-1/2**17)`？

**答案**：输出格式是 `[1,0,17]`，有符号定点能精确表示 -1.0 但不能表示 +1.0（见 u1-l4 的不对称性）。正弦峰值恰为 +1.0，若不压一点会在峰值饱和，产生可观的系统误差；乘一个略小于 1 的常数把峰值压到 +1 之下，代价仅是整体增益微降。

---

### 4.2 模板代码生成

#### 4.2.1 概念说明

有了两张定点整数表，下一步是把它们「印」进一个可综合的 VHDL 文件。psi_fix 不手写这些文件——`sin18b/sqrt18b/inv18b/gaussify20b` 四个组件的实体高度同构（都是「读 ROM + 例化同一个 calc 内核」），只有 entity 名、几个格式常量、ROM 内容不同。于是作者把公共骨架抽成一个**模板文件**，里面用尖括号占位符标出可变位置，再用 Python 的 `str.replace` 逐个替换。

这套做法的好处：

- **可审查**：模板就是合法的 VHDL（把占位符当注释看也能读懂），生成逻辑只是字符串替换，没有黑魔法。
- **DRY**：四个组件 + 未来的新函数共用一份模板，改骨架只改一处。
- **与内核解耦**：模板只负责「读 ROM + 例化 calc」，ROM 读时序与近似计算完全交给 u8-l1 的手写内核，二者各司其职。

#### 4.2.2 核心流程

组件实体的生成流程：

```
1. 读 snippets/psi_fix_lin_approx_tmpl.vhd 文本
2. content.replace("<ENTITY_NAME>", "psi_fix_lin_approx_"+cfg.name)
3. 逐个替换 <IN_WIDTH>/<OUT_WIDTH>/<IN_FMT>/<OUT_FMT>/<GRAD_FMT>/<OFFS_FMT>/<TABLE_SIZE>/<TABLE_WIDTH>
4. 把每对 (gradInt, offsInt) 拼成一行:
       std_logic_vector(to_signed(gradInt, gradBits) & to_signed(offsInt, offsBits)),
   最后一行去掉尾逗号, 合并成 <TABLE_CONTENT>
5. 写到 hdl/<ENTITY_NAME>.vhd
```

测试台的生成流程几乎一样（换一个模板），额外跑一次 `Approximate` 产出 `stimuli.txt`/`response.txt` 两个比对文本。

#### 4.2.3 源码精读

**组件模板骨架**——看占位符就懂生成器要填什么：

[model/snippets/psi_fix_lin_approx_tmpl.vhd:20-32](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/snippets/psi_fix_lin_approx_tmpl.vhd#L20-L32) —— 实体声明：`<ENTITY_NAME>` 决定实体名，`<IN_WIDTH>-1`/`<OUT_WIDTH>-1` 决定端口位宽，注释里的 `<IN_FMT>`/`<OUT_FMT>` 是给人看的格式说明。

[model/snippets/psi_fix_lin_approx_tmpl.vhd:40-52](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/snippets/psi_fix_lin_approx_tmpl.vhd#L40-L52) —— 常量与表：`<IN_FMT>` 等被替换成 `(0, 0, 20)` 这样的 record 字面量；`Table_t` 数组类型与 `Table_c` 常量，`<TABLE_CONTENT>` 处会被展开成上千行 `std_logic_vector(...)`。

[model/snippets/psi_fix_lin_approx_tmpl.vhd:60-90](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/snippets/psi_fix_lin_approx_tmpl.vhd#L60-L90) —— 例化内核 + ROM 读进程：`i_calc` 把格式与表规模通过 generic 传给 [hdl/psi_fix_lin_approx_calc.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_calc.vhd)（u8-l1），`addr_table_o`/`data_table_i` 是表接口；`p_table` 是一个时钟周期的寄存式 ROM 读，把 `Table_c(TableAddr)` 打一拍送到 `data_table_i`。

**生成器本体**：

[model/psi_fix_lin_approx.py:251-297](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L251-L297) —— `GenerateEntity`：

- L258 实体名 = `"psi_fix_lin_approx_" + cfg.name`，所以 `name="sin18b"` 直接决定文件名。
- L261-262 读模板全文。
- L265-273 一串 `content.replace` 把占位符换成 `str(fmt)`、`psi_fix_size(fmt)` 等。
- L276-289 关键的表行构造：按 `offsFmt.s`/`gradFmt.s` 选 `to_signed`/`to_unsigned`，用 `psi_fix_get_bits_as_int` 取位模式整数，宽度填 `psi_fix_size(fmt)`。
- L290 `std_logic_vector({g} & {o})` —— **gradient 在高位、offset 在低位**，这个拼接顺序必须和 u8-l1 内核拆 `data_table_i` 的顺序一致，是位真的硬约束。
- L291 去掉最后一行的尾逗号（否则 VHDL 聚合体语法报错）。
- L296 写盘到 `hdl/`。

**测试台生成器**：

[model/psi_fix_lin_approx.py:299-341](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L299-L341) —— `GenerateTb`：读 [model/snippets/psi_fix_lin_approx_tb_tmpl.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/snippets/psi_fix_lin_approx_tb_tmpl.vhd)，替换 `<ENTITY_NAME>`/`<IN_FMT>`/`<OUT_FMT>`，然后在 `validRange` 上 `linspace` 一批输入，跑 `Approximate` 得到期望输出，用 `psi_fix_get_bits_as_int` 写成 `stimuli.txt`/`response.txt`。这两个文本是测试台逐位比对的黄金数据。

**测试台模板**——和 u3-l2 的协同仿真套路同源：

[model/snippets/psi_fix_lin_approx_tb_tmpl.vhd:82-119](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/snippets/psi_fix_lin_approx_tb_tmpl.vhd#L82-L119) —— `p_stimuli` 用 `ApplyTextfileContent` 把 `stimuli.txt` 重放成 DUT 输入，`p_response` 用 `CheckTextfileContent` 把 DUT 输出与 `response.txt` 逐行比对，不符即由 `psi_tb` 打印 `###ERROR###`。注意测试台通过 generic `stimuli_dir_g` 接收数据目录（L26-27），所以同一个测试台结构可以指向任一函数的 `stimuli/response`。

#### 4.2.4 代码实践

**实践目标**：对照「模板 → 生成器 → 产物」三者，确认占位符被正确替换、表拼接顺序正确。

**操作步骤**：

1. 在 [model/snippets/psi_fix_lin_approx_tmpl.vhd:40-45](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/snippets/psi_fix_lin_approx_tmpl.vhd#L40-L45) 找到占位符 `<IN_FMT>`/`<GRAD_FMT>`/`<TABLE_SIZE>`/`<TABLE_WIDTH>`。
2. 在生成器 [model/psi_fix_lin_approx.py:265-273](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L265-L273) 找到对应的 `replace` 语句。
3. 在产物 [hdl/psi_fix_lin_approx_sin18b.vhd:27-32](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_sin18b.vhd#L27-L32) 看替换结果：`InFmt_c := (0,0,20)`、`GradFmt_c := (1,3,8)`、`TableSize_c := 2048`、`TableWidth_c := 32`。

**需要观察的现象**：

- 占位符被换成了 record 字面量与整数常量；
- `TableWidth_c = 32 = psi_fix_size(GradFmt_c) + psi_fix_size(OffsFmt_c) = 12 + 20`；
- 表行是 `to_signed(grad, 12) & to_signed(offs, 20)`，gradient 在左（高位）。

**预期结果**：模板、生成器、产物三方完全自洽；`TableWidth_c` 等于两张表位宽之和，证明 L273 的 `<TABLE_WIDTH>` 替换与 L290 的 `g & o` 拼接用的是同一套位宽计算。如要本地验证，可临时在某 `replace` 后加一行 `print(content.count("<"))`，正常应只剩 0（即所有占位符都已替换）——具体运行待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 L291 要专门去掉最后一行的尾逗号？

**答案**：VHDL 的命名聚合体（`constant Table_c : Table_t := (a, b, c);`）允许每项后跟逗号，但**最后一项后不能有逗号**，否则语法错。生成器对所有项用统一模板加了逗号，所以必须把最后一行的逗号抹掉。

**练习 2**：如果把 L290 的 `g & o` 误写成 `o & g`（offset 在高位），编译能过吗？功能对吗？

**答案**：能编译过（位宽没变），但功能会错——u8-l1 的 calc 内核按「gradient 高位 | offset 低位」拆 `data_table_i`，拼接顺序反了会把 offset 当 gradient、gradient 当 offset，逐位比对必然报 `###ERROR###`。这正是「模型与 RTL 必须共用同一套格式/拼接约定」的意义。

---

### 4.3 生成式组件族

#### 4.3.1 概念说明

把 4.1、4.2 合起来，psi_fix 形成了一种**生成式组件族**的维护模式：

- **一个手写内核**：`hdl/psi_fix_lin_approx_calc.vhd`（u8-l1），不含任何函数-specific 内容，只做「拆索引/余数→乘→加→resize」的通用流水。
- **一组配置**：`CONFIGS` 里的 4 项，每项 = 一个数学函数 + 一组定点格式。
- **一个生成器入口**：`if __name__ == "__main__"` 遍历 `CONFIGS.all`，对每项调用 `GenerateEntity` + `GenerateTb`，把产物写到 `hdl/` 与 `testbench/`。

于是 `sin / sqrt / inv / gaussify` 四个看起来无关的组件，本质是「同一个内核 + 四张不同的表」。要新增一个函数（比如 `tan`），不必写一行 RTL，只需：写一份配置 → 跑一次脚本 → 把生成的 `.vhd`、测试台、`stimuli/response.txt` 提交 → 在 `sim/config.tcl` 注册源与测试台。

注意一个与 FIR/CIC（u6/u7）的重要区别：lin_approx 测试台的比对数据是**生成一次、提交进仓库**的（`testbench/psi_fix_lin_approx_tb/<name>/{stimuli,response}.txt`），**不**像 FIR 那样每次回归前由 `pre_script` 现场重生。原因有二：一是这些数据由生成器与表绑定，确定性极强、重算无意义；二是组件实体本身就是生成产物，表与测试数据必须同源提交，否则版本会错配。

#### 4.3.2 核心流程

新增一个生成式函数的完整闭环：

```
1. 用 Design(cfg) 试配置, 确认 offsFmt/gradFmt 够用、误差可接受
2. 把定稿的配置加进 CONFIGS (取一个 name, 如 "tan18b")
3. 运行 python3 model/psi_fix_lin_approx.py
   -> 生成 hdl/psi_fix_lin_approx_tan18b.vhd
   -> 生成 testbench/psi_fix_lin_approx_tb/tan18b/*.{vhd,txt}
4. 在 sim/config.tcl:
   - add_sources -tag src 里加生成的 .vhd
   - add_sources -tag tb 里加生成的 _tb.vhd
   - create_tb_run + tb_run_add_arguments("-gstimuli_dir_g=...")
5. 跑回归, 确认无 ###ERROR###
```

维护已有函数（如把 sin 的精度从 18 位提到 20 位）：只改 `CONFIGS.Sin18Bit` 的格式与段数 → 重跑脚本 → 提交重新生成的 HDL 与数据 → 回归。**手写内核 `calc` 完全不用动**。

#### 4.3.3 源码精读

**生成入口**——一句话启动全族生成：

[model/psi_fix_lin_approx.py:365-370](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L365-L370) —— `__main__`：遍历 `CONFIGS.all`，对每项 `cfg` 实例化后调 `GenerateEntity("../hdl")` 与 `GenerateTb("../testbench/psi_fix_lin_approx_tb/"+cfg.name)`。`cfg.name` 同时决定实体名后缀、文件名与测试台子目录名，是贯穿三处的单一索引。

**四份配置 → 四个产物**的对照表（核心事实）：

| 配置项（CONFIGS） | `name` | function（要点） | 生成实体 | 关键格式 |
| --- | --- | --- | --- | --- |
| `Sin18Bit` ([L79-86](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L79-L86)) | `sin18b` | `sin(x*2π)`，整周期 | `psi_fix_lin_approx_sin18b.vhd` | in(0,0,20)/out(1,0,17)，2048 段 |
| `Sqrt18Bit` ([L87-95](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L87-L95)) | `sqrt18b` | `sqrt(x)`，限 \([0.25,\approx1)\) | `psi_fix_lin_approx_sqrt18b.vhd` | out(0,0,17)，512 段 |
| `Gaussify20Bit` ([L96-104](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L96-L104)) | `gaussify20b` | 查表的 `_Gaussify`（正态 CDF 反函数） | `psi_fix_lin_approx_gaussify20b.vhd` | in/out(1,0,19)，1024 段 |
| `Invert18Bit` ([L105-113](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L105-L113)) | `inv18b` | `1/x`，限 \([1,\approx2)\) | `psi_fix_lin_approx_inv18b.vhd` | in(0,1,18)/out(0,0,18)，1024 段 |

> `hdl/` 下还有一个 `psi_fix_lin_approx_sin18b_dual.vhd`（双通道版），它不在 `CONFIGS.all` 的标准四项里，是按特殊需求派生的变体，本讲不展开。

**回归注册**——生成产物如何进入 CI：

[sim/config.tcl:72-77](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L72-L77) —— 把内核与四个生成实体声明为待编译源（`calc` 必须先于其他，因为它们例化它）。

[sim/config.tcl:110-114](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L110-L114) —— 声明四个生成测试台为 tb 源。

[sim/config.tcl:169-192](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L169-L192) —— 为每个测试台 `create_tb_run`，用 `tb_run_add_arguments "-gstimuli_dir_g=$dataDir"` 把生成好的数据目录喂给测试台的 generic。注意这里**没有** `tb_run_add_pre_script`（对比 [sim/config.tcl:195](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L195) 的 FIR 测试用了 pre_script），印证了「数据生成一次、提交进仓库」的设计。

**平行样例——LUT 生成器**：另一个「Python 表 + 模板 → VHDL」的例子，但更简单（整表查表，非分段线性）：

[model/psi_fix_lut.py:50-90](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lut.py#L50-L90) —— `Generate`：读 [model/snippets/psi_fix_lut_tmpl.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/snippets/psi_fix_lut_tmpl.vhd)，替换 `<ENTITY_NAME>`/`<OUT_FMT>`/`<SIZE>`/`<TABLE_CONTENT>`，每项一行 `std_logic_vector(to_signed/unsigned(coefInt, coefBits))`。它的模板 [model/snippets/psi_fix_lut_tmpl.vhd:20-49](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/snippets/psi_fix_lut_tmpl.vhd#L20-L49) 是单口寄存式 ROM（带 `rom_style_g` 让综合器选 block/distributed RAM）。它与 lin_approx 共享同一套生成哲学，只是表内容与端口拓扑不同。

#### 4.3.4 代码实践

**实践目标**：把「同一个内核 + 不同表」落实到具体的「配置 → 组件 → 测试台 → 回归」四元组上，回答本讲开篇的问题。

**操作步骤**：

1. 打开 [model/psi_fix_lin_approx.py:365-370](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L365-L370)，确认 `__main__` 遍历的就是 `CONFIGS.all` 这 4 项。
2. 逐项填写下表（答案见「预期结果」）：

   | 组件文件（hdl/） | 由哪个 CONFIGS 生成 | validRange |
   | --- | --- | --- |
   | `psi_fix_lin_approx_sin18b.vhd` | ? | ? |
   | `psi_fix_lin_approx_sqrt18b.vhd` | ? | ? |
   | `psi_fix_lin_approx_inv18b.vhd` | ? | ? |
   | `psi_fix_lin_approx_gaussify20b.vhd` | ? | ? |

3. 在 [sim/config.tcl:169-192](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L169-L192) 确认每个生成测试台都对应一项 `create_tb_run`，且都用 `-gstimuli_dir_g` 指向各自的数据目录、无 pre_script。

**需要观察的现象**：四个组件实体结构完全同构（都是 [tmpl.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/snippets/psi_fix_lin_approx_tmpl.vhd) 那一套），差别仅在 6 个常量与 ROM 内容；四个测试台也同构，差别仅在 `InFmt_c/OutFmt_c` 与数据目录。

**预期结果（对照答案）**：

| 组件文件 | CONFIGS | validRange |
| --- | --- | --- |
| `sin18b` | `Sin18Bit`（L79） | 默认全范围（未显式给） |
| `sqrt18b` | `Sqrt18Bit`（L87） | `(0.25, (1-2^-17)^2)` |
| `inv18b` | `Invert18Bit`（L105） | `(1, 2-2^-20)` |
| `gaussify20b` | `Gaussify20Bit`（L96） | `(-1, 1)` |

这一步同时完成了本讲指定的实践任务：**`sin18b/sqrt18b/inv18b/gaussify20b` 四个组件，分别由 `CONFIGS.Sin18Bit/Sqrt18Bit/Invert18Bit/Gaussify20Bit` 生成，它们共享同一个 `psi_fix_lin_approx_calc` 内核与同一份 `psi_fix_lin_approx_tmpl.vhd` 模板，差异只在生成器算出的 offset/gradient 表与四个定点格式常量。**

#### 4.3.5 小练习与答案

**练习 1**：lin_approx 测试台为什么不像 FIR 那样用 `tb_run_add_pre_script` 现场重生数据？

**答案**：lin_approx 的实体本身就是生成产物，ROM 表与测试数据必须由同一次 `GenerateEntity/GenerateTb` 同源产出、一起提交，才能保证版本一致；数据是确定性的，每次回归重算毫无价值。FIR 的系数/激励由 pre_script 按固定随机种子现场生成，是因为其数据量大且与 generic 参数矩阵耦合，提交进仓库不划算。

**练习 2**：若想把 `sin18b` 的段数从 2048 提到 4096 以降低近似误差，需要改 RTL 吗？

**答案**：不需要动任何手写 RTL。只需改 `CONFIGS.Sin18Bit` 的 `points=4096`（并视情况调整 `gradFmt/offsFmt` 容纳新的导数范围），重跑 `python3 model/psi_fix_lin_approx.py`，把重新生成的 `hdl/psi_fix_lin_approx_sin18b.vhd` 与 `testbench/.../sin18b/*.txt` 提交即可。内核 `calc` 完全复用——这正是生成式组件族的核心收益。

## 5. 综合实践

把本讲三件事串起来：**算表 → 生成组件 → 生成测试数据 → 注册回归**。

任务：假装要给 psi_fix 增加一个 `cos18b` 余弦近似组件（其实它与 sin 只差相位，这里仅作练习载体）。

1. **设计配置**：参考 [model/psi_fix_lin_approx.py:79-86](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L79-L86)，写一份 `Cos18Bit = psi_fix_lin_cfg_settings(function=lambda x: np.cos(x*2*np.pi)*(1-1/2**17), inFmt=psi_fix_fmt_t(0,0,20), outFmt=psi_fix_fmt_t(1,0,17), offsFmt=psi_fix_fmt_t(1,0,19), gradFmt=psi_fix_fmt_t(1,3,8), points=2048, name="cos18b")`，并把它加入 `CONFIGS.all`。
2. **预测产物**：写出将要生成的文件名（`hdl/psi_fix_lin_approx_cos18b.vhd`、`testbench/psi_fix_lin_approx_tb/cos18b/psi_fix_lin_approx_cos18b_tb.vhd` 及 `stimuli.txt`/`response.txt`），并预测表第 0 项的 gradient——cos 在 \(x=0\) 处导数为 0，所以 `to_signed(0, 12)` 应出现在高位。
3. **补回归**：参照 [sim/config.tcl:72-77](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L72-L77) 与 [sim/config.tcl:169-192](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L169-L192)，写出要新增的 `add_sources` 两行与 `create_tb_run`/`tb_run_add_arguments` 一段。
4. **验证位真**：跑回归后，确认 `psi_fix_lin_approx_cos18b_tb` 不打印 `###ERROR###`（具体运行待本地验证）。

这个练习覆盖了本讲全部三个最小模块：写配置即「表生成算法」、预测文件名与表项即「模板代码生成」、补 `config.tcl` 即「生成式组件族」的维护闭环。

## 6. 本讲小结

- `model/psi_fix_lin_approx.py` 是**位真模型 + 代码生成器**二合一：构造函数对一个数学函数在段中心采样、用 scipy `derivative` 求导、用 `psi_fix_from_real(err_sat=False)` 量化出 offset/gradient 两张定点整数表。
- `GenerateEntity`/`GenerateTb` 用最朴素的 `str.replace` 把 `snippets/*.vhd` 模板里的 `<占位符>` 换成实体名、格式常量与表内容；表行严格按「gradient 高位 | offset 低位」拼接，这是与 u8-l1 内核的位真硬约束。
- 四个组件 `sin18b/sqrt18b/inv18b/gaussify20b` 由 `CONFIGS` 的四项配置驱动，共享同一个手写内核 `psi_fix_lin_approx_calc` 与同一份模板，差异仅在表与格式常量。
- lin_approx 测试台的比对数据是**生成一次、提交进仓库**的（`stimuli/response.txt`），通过 generic `stimuli_dir_g` 喂入，**不**用 `pre_script` 现场重生——这与 FIR/CIC 形成对照。
- `psi_fix_lut.py` + `psi_fix_lut_tmpl.vhd` 是同一套「Python 表 → 模板 → VHDL」哲学在整表查表场景的平行样例。
- 维护生成式组件的核心收益：改精度只动配置 + 重跑脚本，**手写 RTL 零修改**。

## 7. 下一步学习建议

- **下一讲 u8-l3**（sqrt/inv/pol2cart_approx 函数实现）：本讲的 `sqrt18b/inv18b` 是「分段线性近似」路线，u8-l3 会讲 `hdl/psi_fix_sqrt.vhd`、`hdl/psi_fix_inv.vhd` 这些**迭代/专用**实现，以及 `psi_fix_pol2cart_approx`——可以对照「同一数学问题的两条实现路线（近似 vs 迭代）」在资源/精度上的取舍。
- **若关心另一个生成器**：阅读 `model/psi_fix_lut.py` 与 `model/snippets/psi_fix_lut_tmpl.vhd`，并搜索仓库里由它生成的 LUT 组件，巩固「表→模板→VHDL」模式。
- **若关心协同仿真的另一面**：回到 u3-l2 与 [sim/config.tcl:194-199](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L194-L199) 的 FIR `pre_script`，对比「数据现场重生」与「数据提交进仓库」两种策略各自的适用场景。
- **想动手**：照第 5 节综合实践，真的加一个 `cos18b` 配置并跑生成，把整条「配置 → 表 → 组件 → 测试 → 回归」链路走一遍。
