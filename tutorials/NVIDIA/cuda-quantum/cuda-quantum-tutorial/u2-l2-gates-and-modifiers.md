# 量子门与修饰符：ctrl、neg 与自定义作用

## 1. 本讲目标

本讲在「量子类型系统」（u2-l1）之后，正式进入「门」的世界。读完本讲，你应该能够：

1. 用 `h / x / y / z / s / t`、旋转门 `rx / ry / rz / r1`、`u3`、`swap` 写出量子内核，并知道它们在源码里是如何被「批量生成」的。
2. 理解 CUDA-Q 的**修饰符**机制：用 `<cudaq::ctrl>`、`<cudaq::adj>` 把一个单比特门提升为受控门 / 伴随门；同时搞清楚一个常见误解——**并不存在 `<cudaq::neg>` 修饰符**，负控（control on |0⟩）是写在量子比特上的 `!` 操作符。
3. 掌握「把一整个子内核当作受控 / 伴随来施加」的另一套写法：`cudaq::control`、`cudaq::adjoint`、`cudaq::compute_action`，并理解它与 `<ctrl>` 修饰符的等价关系。

本讲的实践任务是用**两种写法**实现同一个 Toffoli（双控非）门，并采样验证真值表。

## 2. 前置知识

- **u2-l1** 引入的量子类型：`qubit = qudit<2>`、`qvector`、`qview`。门操作的参数都是 `qubit&`（或一组比特的视图），本讲不再重复类型细节。
- **u1-l4** 引入的执行模型：门函数（如 `x(q)`）并不真正「执行」量子操作，而是把「门名 + 参数 + 控制比特 + 目标比特」记录到全局单例 **ExecutionManager**，最终交由后端解释。本讲的全部门函数都遵循这一约定。
- 受控门（controlled-`U`）的数学定义：

  \[
  C(U) = |0\rangle\langle 0| \otimes I + |1\rangle\langle 1| \otimes U
  \]

  即「控制比特为 |1⟩ 时才对目标施加 `U`」。负控（control on |0⟩）则是：

  \[
  C_{\neg}(U) = |1\rangle\langle 1| \otimes I + |0\rangle\langle 0| \otimes U = (X \otimes I)\, C(U)\, (X \otimes I)
  \]

  这个等式直接解释了源码里「负控 = 前后各包一层 X」的实现。

- 旋转门的定义（本讲会用到）：

  \[
  R_x(\theta) = e^{-i\theta X/2},\quad R_y(\theta) = e^{-i\theta Y/2},\quad R_z(\theta) = e^{-i\theta Z/2}
  \]

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `runtime/cudaq/qis/modifiers.h` | 全部修饰符的**类型标签**声明：`base`、`ctrl`、`adj`。整个文件只有前向声明，是修饰符机制的「目录」。 |
| `runtime/cudaq/qis/qubit_qis.h` | 默认量子指令集（QIS）的主体。门的宏定义、`oneQubitApply` 修饰符分派逻辑、`cnot/cx/ccx` 等语法糖、`control/adjoint/compute_action` 组合算子、`CUDAQ_REGISTER_OPERATION` 自定义门宏，全在这里。 |
| `runtime/cudaq/qis/qudit.h` | 量子比特本身。负控的状态 `isNegativeControl`、`negate()`、`operator!()` 都在这里。 |
| `runtime/cudaq/qis/qkernel.h` | `qkernel` 包装器。当你把一个内核**作为值**传递（而不是直接调用）时——比如交给 `cudaq::control`——就需要它。 |

## 4. 核心概念与源码讲解

### 4.1 基本门与旋转门

#### 4.1.1 概念说明

CUDA-Q 的「默认逻辑门集」分成三档：

- **无参数单比特门**：`h, x, y, z, s, t`。
- **带一个角度参数的单比特门**：`rx, ry, rz, r1`。
- **通用单比特门**：`u3(theta, phi, lambda)`；以及两比特的 `swap`。

这些门在源码里**不是一个个手写的**，而是用两个预处理宏 `CUDAQ_QIS_ONE_TARGET_QUBIT_` 和 `CUDAQ_QIS_PARAM_ONE_TARGET_` 批量「印」出来的。每个门在源码里只是一个很薄的模板函数，真正干活的是共用的 `oneQubitApply` / `oneQubitSingleParameterApply`。

理解这一点很重要：门的「名字」只是一个字符串（如 `"x"`、`"ry"`），函数体把它连同比特 id 一起交给 ExecutionManager。这与 u1-l4 讲过的「门函数只记录指令」完全一致。

#### 4.1.2 核心流程

以 `x(q)` 为例，调用链是：

1. `x<base>(q)` 命中宏生成的模板 `void x(QubitArgs&... args)`。
2. 转发到 `oneQubitApply<qubit_op::xOp, base>(q)`。
3. `oneQubitApply` 把每个比特映射成 `QuditInfo{id, levels}`，按修饰符分派。
4. 对 `base` 修饰符：对传入的**每一个**比特都单独施加该门（广播，broadcast）。
5. 最终调用 `getExecutionManager()->apply(gateName, /*params*/{}, /*controls*/{}, {qubit})`。

带角度的门（如 `ry(theta, q)`）多一步：把 `theta` 转成 `double` 放进 `parameters` 向量。

#### 4.1.3 源码精读

门的「名字标签」由 `ConcreteQubitOp` 宏生成，每个标签把 C++ 类型映射成一个字符串：

[runtime/cudaq/qis/qubit_qis.h:L40-L51](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L40-L51) —— 定义 `h/x/y/z/s/t/rx/ry/rz/r1/u3` 的 `Op` 结构体，每个都带一个 `name()` 返回门名字符串。

`oneQubitApply` 是整个门体系的「分派中心」：

[runtime/cudaq/qis/qubit_qis.h:L63-L85](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L63-L85) —— 模板参数 `mod` 默认是 `base`；`base` 分支对每个比特分别 `apply`，这就是「`h(qvector)` 会对整条寄存器逐个施加 H」的实现。

批量生成无参数门的宏：

[runtime/cudaq/qis/qubit_qis.h:L138-L174](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L138-L174) —— `CUDAQ_QIS_ONE_TARGET_QUBIT_(h)` 等调用为 `h/x/y/z/t/s` 各自展开出三个重载（单/多比特门 + 寄存器广播），随后第 169–174 行实例化。

带角度的门与旋转门：

[runtime/cudaq/qis/qubit_qis.h:L226-L249](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L226-L249) —— `CUDAQ_QIS_PARAM_ONE_TARGET_` 宏为 `rx/ry/rz/r1` 展开「角度 + 比特」的签名。

此外，源码还提供了一批**语义糖**——它们只是把 `<cudaq::ctrl>` 写法包了一层更常见的名字：

[runtime/cudaq/qis/qubit_qis.h:L339-L368](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L339-L368) —— `cnot/cx/cy/cz/ch/cs/ct`（单控两比特门）、`ccx`（双控非，即 Toffoli）、`crx/cry/crz/cr1`（带角度的受控旋转）、`sdg/tdg`（`s/t` 的伴随）。

#### 4.1.4 代码实践

**实践目标**：直观感受「门 = 名字字符串 + 比特 id」的执行模型，以及寄存器广播。

**操作步骤**：

1. 新建 `broadcast.cpp`，写入下面的内核（示例代码）：

   ```cpp
   #include <cudaq.h>

   __qpu__ void three_h() {
     cudaq::qvector q(3);
     h(q);          // 广播：等价于 h(q[0]); h(q[1]); h(q[2]);
     mz(q);
   }

   int main() {
     auto counts = cudaq::sample(three_h);
     counts.dump();
   }
   ```

2. 编译运行：`nvq++ broadcast.cpp -o broadcast.x && ./broadcast.x`。

**需要观察的现象**：采样分布应近似均匀地出现在 `000/001/.../111` 这 8 个比特串上（每个约 12.5%）。

**预期结果**：3 个比特都被置于均匀叠加态 \(|+\rangle^{\otimes 3}\)，所以 8 个基态等概率出现。这验证了 `h(qvector)` 的「逐比特广播」语义。

#### 4.1.5 小练习与答案

**练习 1**：`ry(M_PI, q)` 等价于哪个简单门？为什么？

**参考答案**：等价于 `x(q)`。因为 \(R_y(\pi) = e^{-i\pi Y/2} = \cos(\pi/2)I - i\sin(\pi/2)Y = -iY\)，而 \(-iY = X\)（相差一个全局相位）。所以二者在采样分布上完全一致。

**练习 2**：为什么源码要用宏 `CUDAQ_QIS_ONE_TARGET_QUBIT_` 批量生成 `h/x/y/z/t/s`，而不是手写 6 份？

**参考答案**：因为这 6 个门的「形状」完全一致（都是「无参数、可广播、可受控」），只有名字字符串不同。宏把「名字」作为参数，避免 6 份几乎一模一样的样板代码，同时保证 6 个门的修饰符行为完全统一（修一处即修六处）。

---

### 4.2 修饰符 ctrl / adj 与负控（`!`）

#### 4.2.1 概念说明

CUDA-Q 的修饰符是一组**空类型标签**，作为模板参数传给门函数，用来改变门的施加方式：

[runtime/cudaq/qis/modifiers.h:L11-L22](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/modifiers.h#L11-L22) —— 只声明了三个标签：`base`（默认，广播施加）、`ctrl`（受控）、`adj`（伴随 / 逆）。

> ⚠️ **重要澄清（本讲最易踩坑点）**：很多文档会把负控叫成「`neg` 修饰符」，但 `modifiers.h` 里**根本没有 `neg` 这个类型**。负控不是修饰符，而是**写在量子比特上**的 `!` 操作符（即 `qudit::operator!()`）。修饰符只有 `base/ctrl/adj` 三种，「neg」通过 `!q` 实现。本讲标题里的「neg」指的就是这个负控语法。

用法对照：

| 写法 | 含义 |
|---|---|
| `x(q)` | 默认 `base`，施加 X |
| `x<cudaq::ctrl>(c, t)` | `ctrl`，c 为 |1⟩ 时翻转 t（CNOT） |
| `x<cudaq::ctrl>(c1, c2, t)` | 多控制：c1=c2=1 时翻转 t（Toffoli） |
| `x<cudaq::ctrl>(!c, t)` | 负控：c 为 |0⟩ 时翻转 t |
| `s<cudaq::adj>(q)` | 伴随：施加 `s` 的逆（即 `sdg`） |

#### 4.2.2 核心流程

`oneQubitApply` 的修饰符分派逻辑（这是本模块的核心）：

- 若 `mod == base`：对每个比特施加该门（广播），返回。
- 否则（`ctrl` 或 `adj`）：把**前 N−1 个比特当控制**，**最后一个比特当目标**。
- 对每个控制比特，检查它是否被 `!` 标成负控：
  - 若是，先施加一个 `x`（把 |0⟩↔|1⟩ 翻转，使「负控」化为「正控」）；
  - 施加真正的受控门；
  - 再施加一个 `x` 复位控制比特；
  - 最后把比特上的负控标记清掉（自动复位）。
- `adj` 与 `ctrl` 的区别仅在于给 `apply` 传一个 `isAdjoint=true` 标志。

数学上，「前后包 X」正是 4.2 前置知识里 \(C_{\neg}(U) = (X\otimes I)\,C(U)\,(X\otimes I)\) 的逐字实现。

#### 4.2.3 源码精读

`ctrl`/`adj` 分支：抽取控制比特与目标比特、施加门：

[runtime/cudaq/qis/qubit_qis.h:L87-L101](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L87-L101) —— 前 N−1 个为控制、最后一个为目标；对负控比特先施加 `x`；调用 `apply(gateName, {}, controls, {target}, isAdjoint)`。`isAdjoint` 由 `std::is_same_v<mod, adj>` 编译期决定。

负控的「包 X + 复位标记」：

[runtime/cudaq/qis/qubit_qis.h:L94-L117](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L94-L117) —— 第 94–97 行对负控比特施加 X；第 104–117 行在门施加后再施加一次 X 复位，并通过 fold 表达式调用 `args.negate()` 把负控标记翻回 `false`。这是一种 RAII 式的「用完即复位」。

负控标记的来源——`operator!` 与 `negate()`：

[runtime/cudaq/qis/qudit.h:L54-L67](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qudit.h#L54-L67) —— `negate()` 翻转 `isNegativeControl` 标记；`operator!()` 是它的语法糖。这说明写 `x<cudaq::ctrl>(!q, r)` 时，`!q` 在构造参数时就把 `q` 标成负控，门函数读到该标记后实现「\(q=|0\rangle\) 时翻转 \(r\)」的语义，并在事后自动复位。

官方文档对负控语法的明确说明：

[docs/sphinx/api/default_ops.rst:L478-L498](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/api/default_ops.rst#L478-L498) —— 默认控制极性为 |1⟩ 触发；用 `!` 前置翻转极性为 |0⟩ 触发；并强调 `!` 只在受控操作里、且只能用于控制比特（不能用于 `swap` 的目标比特）。

#### 4.2.4 代码实践

**实践目标**：对比「正控 CNOT」与「负控 CNOT」的真值表，亲眼看到 `!` 的效果。

**操作步骤**：

1. 写内核（示例代码）：

   ```cpp
   #include <cudaq.h>

   // prepare=0..3 编码 c,t 的初值（bit0=c, bit1=t）
   __qpu__ void cnot_pos_neg(int prepare, bool use_neg) {
     cudaq::qubit c, t;
     if (prepare & 1) x(c);
     if (prepare & 2) x(t);
     if (use_neg) x<cudaq::ctrl>(!c, t);   // 负控：c=|0> 时翻转 t
     else         x<cudaq::ctrl>(c, t);    // 正控：c=|1> 时翻转 t
     mz(c); mz(t);
   }

   int main() {
     for (int p = 0; p < 4; p++) {
       auto cp = cudaq::sample(1000, cnot_pos_neg, p, false);
       auto cn = cudaq::sample(1000, cnot_pos_neg, p, true);
       printf("init=%d  pos=", p); cp.dump();
       printf("        neg=");     cn.dump();
     }
   }
   ```

2. 编译运行：`nvq++ cnot_pos_neg.cpp -o cnpn.x && ./cnpn.x`。

**需要观察的现象**：正控只在 `c=1` 时翻转 `t`；负控只在 `c=0` 时翻转 `t`。二者真值表恰好「镜像」。

**预期结果**（按 `c,t` 顺序，目标比特 `t` 的翻转条件）：

| 初值 (c,t) | 正控后 t 翻转? | 负控后 t 翻转? |
|---|---|---|
| 00 | 否 | **是** |
| 01 | 否 | **是**（t: 1→0）|
| 10 | **是** | 否 |
| 11 | **是**（t: 1→0）| 否 |

> 注：`sample` 返回的比特串字节序与 `mz` 的调用顺序/寄存器命名有关，请以 `dump()` 实际打印为准；若对顺序有疑问，可单独 `mz` 并指定寄存器名（见 u2-l3）。若暂无 nvq++ 环境，本表为「待本地验证」的理论值。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `x<cudaq::ctrl>(!q, r)` 用过之后，`q` 不会一直保持「负控」状态？

**参考答案**：因为 `oneQubitApply` 在施加完门之后，会遍历参数调用 `args.negate()`，把 `isNegativeControl` 翻回 `false`。见 [runtime/cudaq/qis/qubit_qis.h:L104-L117](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L104-L117)。这是一种 RAII 式的「自动复位」，保证下一次再用 `q` 做控制时是干净的。

**练习 2**：把 `s<cudaq::adj>(q)` 和 `sdg(q)` 放进同一个内核分别作用在两个比特上，采样结果会有差别吗？

**参考答案**：不会。`sdg` 在源码里就是 `s<cudaq::adj>` 的语法糖（见 [qubit_qis.h:L367-L368](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L367-L368)），二者生成完全相同的指令（门名 `"s"` + `isAdjoint=true`）。

---

### 4.3 控制语义的另一面：control / adjoint / compute_action

#### 4.3.1 概念说明

`<cudaq::ctrl>` 修饰符只能把**单个内建门**提升为受控。但很多时候我们想把**一整个子内核**（里面可能有几十个门）整体受控或求逆——比如做 Trotter 步进的伴随、或把一个 ansatz 块当作受控模块。CUDA-Q 提供了三个「施加算子」来支撑这种组合：

- `cudaq::control(kernel, ctrl_qubits, args...)` —— 在给定控制比特下施加整个 `kernel`。
- `cudaq::adjoint(kernel, args...)` —— 施加 `kernel` 的伴随。
- `cudaq::compute_action(C, A)` —— 施加 \(C\,A\,C^{\dagger}\)；`compute_dag_action(C, A)` 施加 \(C^{\dagger} A\,C\)。这是量子算法里极常见的「计算-作用-逆计算」模式。

> 说明：CUDA-Q 的 C++ QIS 里**没有一个叫 `apply` 的符号**。本讲的「自定义作用 / apply 算子」指的就是上面这一族把内核「整体施加 / 受控 / 求逆」的函数。它们与 `<ctrl>`/`<adj>` 修饰符是**同一套执行机制的两个入口**：修饰符作用于单个门，`control`/`adjoint` 作用于整个内核。

要把内核「当作值」传给这些函数，离不开 `qkernel` 包装器：

[runtime/cudaq/qis/qkernel.h:L98-L108](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qkernel.h#L98-L108) —— `qkernel` 用来包装被引用（而非直接调用）的 `__qpu__` 内核，编译器会特殊处理它，让运行时能把这些内核「缝合」进宿主内核执行。

#### 4.3.2 核心流程

`cudaq::control` 的实现思路（这是理解它与修饰符等价性的关键）：

1. 收集控制比特的 id，调用 `getExecutionManager()->startCtrlRegion(ctrls)`，告诉 ExecutionManager「接下来所有门都带这些控制比特」。
2. 执行 `kernel(args...)`——此时 kernel 内部的每一个门都被自动「染上」这些控制比特。
3. 调用 `endCtrlRegion(...)` 关闭受控区域。

这等价于在 kernel 的每个门上都加上 `<cudaq::ctrl>(ctrls..., target)`，只是由 ExecutionManager 在区域层面统一处理。`adjoint` 同理，用 `startAdjointRegion()/endAdjointRegion()` 把区域内的所有门整体求逆。

#### 4.3.3 源码精读

`control` 的重载（最常用：一个内核 + 一组控制比特 + 目标参数）：

[runtime/cudaq/qis/qubit_qis.h:L665-L687](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L665-L687) —— 注意概念约束 `isCallableVoidKernel<QuantumKernel, Args...>`：被控制的内核必须返回 `void`。控制比特可以是单个 `qubit`、一个 `qvector`/`qview`，或 `std::vector<std::reference_wrapper<qubit>>`。

`adjoint` 与 `compute_action`：

[runtime/cudaq/qis/qubit_qis.h:L706-L737](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L706-L737) —— `adjoint` 用 `startAdjointRegion/endAdjointRegion` 包住 kernel；`compute_action(c, a)` 展开为 `c(); a(); adjoint(c);`，即 \(C\,A\,C^{\dagger}\)。

官方示例里有一段非常清楚的「等价性」对照——同一个双控非，先用 `cudaq::control` 写，再用 `<ctrl>` 写：

[docs/sphinx/examples/cpp/building_kernels.cpp:L87-L109](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/building_kernels.cpp#L87-L109) —— `kernel6` 用 `cudaq::control(x_kernel, control_vector, target)`（`x_kernel` 内部是单比特 `x`，`control_vector` 是 2 比特寄存器），等价于 `kernel7` 里的 `x<cudaq::ctrl>(qvector[0], qvector[1])` 配合目标比特。这正是本讲综合实践要复刻的「两种写法」。

`compute_action` 的真实用例（H2 基态 ansatz）：

[docs/sphinx/examples/cpp/other/compute_actions.cpp:L48-L65](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/other/compute_actions.cpp#L48-L65) —— 用 `cudaq::compute_action(compute_lambda, action_lambda)` 替代手写的 `C ... C†` 阶梯，代码更短且不易写错逆序。

#### 4.3.4 代码实践

**实践目标**：用 `cudaq::control` 把一个「单比特 X」子内核提升为受控，验证它和 `x<cudaq::ctrl>` 产出相同分布。

**操作步骤**：

1. 写内核（示例代码，对照 building_kernels.cpp 的 kernel6/kernel7）：

   ```cpp
   #include <cudaq.h>

   __qpu__ void x_kernel(cudaq::qubit &t) { x(t); }

   __qpu__ void via_control() {
     cudaq::qubit c, t;
     h(c);                             // 让 c 处于叠加态
     cudaq::control(x_kernel, c, t);   // 等价于 x<cudaq::ctrl>(c, t)
     mz(c); mz(t);
   }

   __qpu__ void via_modifier() {
     cudaq::qubit c, t;
     h(c);
     x<cudaq::ctrl>(c, t);
     mz(c); mz(t);
   }

   int main() {
     auto r1 = cudaq::sample(via_control);
     auto r2 = cudaq::sample(via_modifier);
     r1.dump(); r2.dump();
   }
   ```

2. 编译运行：`nvq++ two_ways.cpp -o two_ways.x && ./two_ways.x`。

**需要观察的现象**：两个内核的采样分布应当完全一致。`c` 初始 |0⟩，H(c) 后 (|0⟩+|1⟩)/√2，受控 X 后 `c,t` 纠缠为 (|00⟩+|11⟩)/√2。

**预期结果**：`r1` 与 `r2` 都只在 `00` 和 `11` 两个比特串上各约 50%（忽略采样涨落）。这就证明了 `cudaq::control` 与 `<cudaq::ctrl>` 在语义上等价。若分布不一致，先检查 `mz` 顺序与字节序（见 u2-l3）。

#### 4.3.5 小练习与答案

**练习 1**：`cudaq::control(x_kernel, c, t)` 和 `x<cudaq::ctrl>(c, t)` 在源码层面走的是同一条「施加」路径吗？

**参考答案**：最终都汇聚到 `getExecutionManager()->apply(...)` 并带上控制比特。差别在「如何带上控制」：修饰符版本在 `oneQubitApply` 里直接把控制比特写进 `apply` 的 `controls` 参数；`cudaq::control` 版本则用 `startCtrlRegion/endCtrlRegion` 在区域层面「染色」，由 ExecutionManager 把区域内的门自动加上控制。对单门而言二者等价；`cudaq::control` 的优势是能一次性控制一整个多门内核。

**练习 2**：为什么 `cudaq::control` 的内核参数概念约束要求内核返回 `void`？

**参考答案**：见 [runtime/cudaq/qis/qubit_qis.h:L650-L653](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L650-L653) 的 `isCallableVoidKernel`。受控施加是把内核的「门序列」整体染上控制比特，它语义上是一段「过程」而非「函数返回值」；量子内核的返回值（测量结果）由 `mz`/`sample` 通路单独处理，不走内核返回值。因此约束返回 `void` 以避免歧义。

---

## 5. 综合实践：用两种写法实现 Toffoli 并验证真值表

**任务**：Toffoli 门（CCNOT）是真控的双控非：\(t \mapsto t \oplus (c_1 \cdot c_2)\)。请用本讲学到的两种方式各实现一遍，遍历 8 个基态输入，采样验证两者真值表完全一致。

**写法 A——多控制 `<cudaq::ctrl>` 修饰符**（或直接用语法糖 `cudaq::ccx`）：

```cpp
// 示例代码
#include <cudaq.h>

// bits 的 bit0=c1, bit1=c2, bit2=t
__qpu__ void toffoli_modifier(int bits) {
  cudaq::qvector q(3);
  if (bits & 1) x(q[0]);   // 准备 c1
  if (bits & 2) x(q[1]);   // 准备 c2
  if (bits & 4) x(q[2]);   // 准备 t
  x<cudaq::ctrl>(q[0], q[1], q[2]);   // 双控非；也可写 cudaq::ccx(q[0],q[1],q[2])
  mz(q);
}
```

**写法 B——`cudaq::control` 把单比特 X 内核提升为双控**（仿照 building_kernels.cpp 的 kernel6）：

```cpp
// 示例代码
__qpu__ void x_kernel(cudaq::qubit &t) { x(t); }

__qpu__ void toffoli_apply(int bits) {
  cudaq::qvector q(3);
  if (bits & 1) x(q[0]);
  if (bits & 2) x(q[1]);
  if (bits & 4) x(q[2]);
  // 用 q[0],q[1] 作为控制寄存器，对 q[2] 施加 x_kernel
  cudaq::control(x_kernel, q.front(2), q[2]);
  mz(q);
}

int main() {
  for (int b = 0; b < 8; b++) {
    auto ra = cudaq::sample(100, toffoli_modifier, b);
    auto rb = cudaq::sample(100, toffoli_apply, b);
    printf("init=%d  modifier=", b); ra.dump();
    printf("        control =");      rb.dump();
  }
}
```

**验证步骤**：

1. 编译：`nvq++ toffoli.cpp -o toffoli.x`。
2. 运行：`./toffoli.x`。
3. 对照真值表：仅当 `c1=c2=1`（`bits` 为 3 或 7）时，目标比特 `t` 翻转；其余情况 `t` 不变。两种写法在每个输入下的输出比特串必须一致。

**预期真值表**（`c1 c2 t` 顺序）：

| 输入 (c1 c2 t) | 输出 (c1 c2 t) | t 翻转? |
|---|---|---|
| 000 | 000 | 否 |
| 001 | 001 | 否 |
| 010 | 010 | 否 |
| 011 | 011 | 否 |
| 100 | 100 | 否 |
| 101 | 101 | 否 |
| 110 | 111 | **是** |
| 111 | 110 | **是** |

> 若你还没有跑通 nvq++（参见 u1-l3），上面的表格是「待本地验证」的理论值；先在纸上按 \(t \oplus (c_1 c_2)\) 推一遍，再上机对照 `dump()` 输出（注意字节序，必要时给 `mz` 指定寄存器名）。

**延伸思考**：把写法 A 里的 `x<cudaq::ctrl>(c1, c2, t)` 换成带一个负控的版本 `x<cudaq::ctrl>(!c1, c2, t)`，真值表会怎样变化？先按 \(X\cdot C(U)\cdot X\) 推一遍，再上机对照。（提示：翻转条件变成「c1=0 且 c2=1 时翻转 t」。）

## 6. 本讲小结

- CUDA-Q 的默认门集（`h/x/y/z/s/t`、`rx/ry/rz/r1`、`u3`、`swap`）由两个宏批量生成，每个门在源码里只是「名字字符串 + 比特 id」的薄封装，统一汇入 `oneQubitApply`。
- **修饰符只有三种**：`base`（默认广播）、`ctrl`（受控）、`adj`（伴随），声明在 `modifiers.h`。**不存在 `neg` 修饰符**。
- 负控（control on |0⟩）通过写在量子比特上的 `!` 操作符（`qudit::operator!`）实现，源码用「前后各包一层 X」来兑现，并在事后自动复位标记——对应数学等式 \(C_{\neg}(U) = (X\otimes I) C(U) (X\otimes I)\)。
- `cnot/cx/ccx/sdg/...` 只是 `<cudaq::ctrl>`/`<cudaq::adj>` 的语法糖；`ccx` 即 Toffoli。
- `cudaq::control`、`cudaq::adjoint`、`cudaq::compute_action` 是「把整个子内核受控 / 求逆 / 套计算-作用-逆计算」的组合算子，与修饰符共享同一套 ExecutionManager 区域机制。
- 选型建议：单门受控用 `<ctrl>` 修饰符最直接；整块内核受控或求逆用 `control`/`adjoint`/`compute_action`。

## 7. 下一步学习建议

- **u2-l3（测量与采样）**：本讲的实践大量依赖 `mz` 与 `sample`，下一讲会正式讲清测量基、寄存器命名、`SampleResult` 的读取接口，帮你解决本讲里反复出现的「字节序 / 比特串顺序」疑问。
- **u2-l4（内核组合与参数传递）**：本讲的 `cudaq::control` 已经把「子内核」推到了台前，下一讲系统讲解内核嵌套调用、参数合成（`ArgumentSynthesis`）。
- **想深入修饰符如何被编译器识别**：可先跳到 **u4-l4（AST Bridge）** 看 `x<cudaq::ctrl>(...)` 是如何从 C++ AST 被翻译成 Quake 受控操作的。
- **想自定义一个全新门**：本讲只提了 `CUDAQ_REGISTER_OPERATION` 宏的入口，完整流程在 **u7-l4（自定义门与自定义算符）**。
