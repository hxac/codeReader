# 量子门与修饰符：ctrl、neg 与自定义作用

## 1. 本讲目标

本讲聚焦 CUDA-Q 编程模型里「量子门（gate）」与「修饰符（modifier）」这一层。学完后你应该能够：

- 正确调用 CUDA-Q 提供的全部内建门（`h/x/y/z/s/t`、旋转门 `rx/ry/rz/r1`、通用门 `u3`、`swap`）。
- 理解 `cudaq::ctrl`、`cudaq::adj` 修饰符把一个单比特门「升级」为受控门、共轭转置门的机制，以及负控（negative control）的真实实现方式。
- 区分两种「施加控制语义」的途径：单门级的模板修饰符 `op<cudaq::ctrl>(...)`，与内核级的 `cudaq::control(...)` / `cudaq::adjoint(...)`。
- 动手实现一个 Toffoli（双控非）门，并用采样验证它的真值表。

> 承接前置讲义：u2-l1 讲清了量子值的类型层次（`qudit`/`qubit`/`qvector`/`qview`）与「不可拷贝」约束。本讲只讲「对这些量子值施加什么操作、怎么施加」，不再重复类型本身。

## 2. 前置知识

- **酉矩阵（unitary matrix）**：量子门（除测量外）对应一个酉矩阵 \(U\)，满足 \(U^{\dagger}U = I\)。作用在 \(n\) 个比特上就是 \(2^n \times 2^n\) 的酉阵。
- **受控门（controlled gate）**：给定一个门 \(U\) 和一个控制比特 \(c\)，受控门 \(C(U)\) 的语义是「仅当 \(c=|1\rangle\) 时才对目标施加 \(U\)」。矩阵上是 \(|0\rangle\langle 0|\otimes I + |1\rangle\langle 1|\otimes U\)。
- **共轭转置（adjoint）**：门的逆操作 \(U^{\dagger}\)。例如 \(S^{\dagger} = S^{-1}\)，记作 `sdg`；\(T^{\dagger}\) 记作 `tdg`。
- **负控（negative control）**：与受控门相反，「仅当 \(c=|0\rangle\) 时才施加 \(U\)」。它等价于在控制比特上做 \(X\cdot C(U)\cdot X\)。
- **修饰符（modifier）**：CUDA-Q 用一组空标记类型（`base`、`ctrl`、`adj`）作为模板参数，告诉门函数「这次调用按普通/受控/共轭来解释」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `runtime/cudaq/qis/modifiers.h` | 定义三个修饰符标记类型 `base`/`ctrl`/`adj`，是整讲的「枚举常量」。 |
| `runtime/cudaq/qis/qudit.h` | 量子比特本身。负控的状态（`isNegativeControl`）、`negate()`、`operator!()` 都在这里。 |
| `runtime/cudaq/qis/qubit_qis.h` | **本讲的主战场**。所有内建门、修饰符分派逻辑、`control()`/`adjoint()`/`compute_action()`、自定义门注册宏都在此文件。 |
| `runtime/cudaq/qis/execution_manager.h` | 声明 `apply(...)`，是所有门最终汇入的「单点收口」。 |
| `runtime/cudaq/qis/qkernel.h` | `qkernel` 包装器，是 `control()`/`adjoint()` 控制整个子内核时所需的「内核可调用对象」类型支撑。 |
| `docs/sphinx/examples/cpp/other/compute_actions.cpp` | 真实示例，展示了 `x<cudaq::ctrl>(...)` 与 `cudaq::compute_action(...)` 两种写法。 |

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：**① 基本门与旋转门**、**② `ctrl`/`adj` 修饰符与负控**、**③ apply 算子与控制语义**。

### 4.1 基本门与旋转门

#### 4.1.1 概念说明

CUDA-Q 的内建门就是一组「按名字识别」的量子指令。在内核里写下 `h(q)`、`ry(0.5, q)`，编译器并不会真的去「执行 C++ 函数体」，而是把这条调用翻译成一条 Quake MLIR 指令（如 `quake.h`、`quake.ry`）。这一点 u1-l4 已经讲过：门函数体只是把「门名 + 比特 + 参数」记录到单例 `ExecutionManager`。

内建门按是否带旋转参数分两类：

- **无参门**：`h`、`x`、`y`、`z`、`s`、`t`（单比特），`swap`（双比特）。
- **带参旋转门**：`rx`、`ry`、`rz`、`r1`（单比特单参数），`u3`（单比特三参数，可表达任意单比特酉）。

它们的矩阵形式（旋转角记作 \(\theta\)）：

\[
H=\frac{1}{\sqrt2}\begin{bmatrix}1&1\\1&-1\end{bmatrix},\quad
X=\begin{bmatrix}0&1\\1&0\end{bmatrix},\quad
R_y(\theta)=\begin{bmatrix}\cos\frac\theta2&-\sin\frac\theta2\\\sin\frac\theta2&\cos\frac\theta2\end{bmatrix},\quad
R_z(\theta)=\begin{bmatrix}e^{-i\theta/2}&0\\0&e^{i\theta/2}\end{bmatrix}
\]

#### 4.1.2 核心流程

所有无参单比特门都由同一个宏批量生成；所有单参数旋转门由另一个宏批量生成。流程是：

1. 用 `ConcreteQubitOp(NAME)` 宏为每个门生成一个带 `name()` 方法的标记类型（`h`→`hOp` 等）。
2. 用 `CUDAQ_QIS_ONE_TARGET_QUBIT_(NAME)` 宏生成用户可调用的 `NAME(...)` 函数（含若干重载）。
3. 函数体把工作转交给模板 `oneQubitApply<QuantumOp, mod>(args...)`，后者最终调用 `getExecutionManager()->apply(...)`。

#### 4.1.3 源码精读

门名的标记类型在这里集中声明——一共 11 个：

[runtime/cudaq/qis/qubit_qis.h:40-51](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L40-L51) —— 用 `ConcreteQubitOp` 宏生成 `h/x/y/z/s/t/rx/ry/rz/r1/u3` 的标记结构体，每个都有一个静态 `name()` 返回字符串名。这段说明：门在 C++ 层只是一个「带名字的空壳类型」，真正的语义在后端模拟器里。

无参单比特门的批量生成宏：

[runtime/cudaq/qis/qubit_qis.h:138-166](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L138-L166) —— `CUDAQ_QIS_ONE_TARGET_QUBIT_(NAME)` 一次生成 4 个重载：单/多比特模板版、用范围作控制比特的版、对整个寄存器逐比特施加的广播版。这段说明：「同一个门名」通过重载覆盖了「单比特、多控制、寄存器广播」三种用法。

宏的实例化点（这就是 `h/x/y/z/t/s` 真正诞生的地方）：

[runtime/cudaq/qis/qubit_qis.h:169-174](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L169-L174) —— 实例化 `h/x/y/z/t/s` 六个无参门。

旋转门的对应宏与实例化：

[runtime/cudaq/qis/qubit_qis.h:226-249](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L226-L249) —— `CUDAQ_QIS_PARAM_ONE_TARGET_` 生成带角度参数的门函数，并实例化 `rx/ry/rz/r1`。注意它比无参版多一个 `ScalarAngle angle` 前置参数。

通用单比特门 `u3` 与双比特 `swap` 是手写的（不走上面两个宏）：

[runtime/cudaq/qis/qubit_qis.h:300-336](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L300-L336) —— `swap` 的定义。`swap(q,r)` 是普通交换门；传入超过 2 个比特时要求修饰符必须是 `ctrl`，前 \(N-2\) 个当控制、最后 2 个当目标，从而支持受控交换。

最后，所有门函数都把工作交给 `oneQubitApply`（无参）或 `oneQubitSingleParameterApply`（带参），它们才是真正「解释修饰符」的地方——见 4.2 节。

#### 4.1.4 代码实践

**实践目标**：亲手调用一遍各类内建门，确认它们能编译并产生采样结果。

**操作步骤**：

1. 新建 `gates_tour.cpp`，写入下面的内核（示例代码）：

   ```cpp
   #include <cudaq.h>
   #include <cmath>

   struct gates_tour {
     void operator()(double theta) __qpu__ {
       cudaq::qvector q(3);
       h(q[0]);
       ry(theta, q[1]);          // 旋转门，带参数
       x(q[2]);
       swap(q[0], q[1]);         // 双比特门
       mz(q);
     }
   };

   int main() {
     auto counts = cudaq::sample(gates_tour{}, 0.5);
     counts.dump();
     return 0;
   }
   ```

2. 用 u1-l3 学到的工具链编译运行：`nvq++ gates_tour.cpp -o gates_tour.x && ./gates_tour.x`。

**需要观察的现象**：终端打印出 3 比特的采样分布（一组 `{比特串 → 次数}`）。

**预期结果**：能成功编译并打印出非空的采样计数（默认 1000 shots）。具体分布「待本地验证」，因为它依赖 `theta=0.5` 下的真实概率。

#### 4.1.5 小练习与答案

**练习 1**：把上面的 `ry(theta, q[1])` 改成 `ry(theta, q[1], q[2])`（一次给两个比特），会发生什么？

**参考答案**：当修饰符是默认的 `base` 且传入多个比特时，旋转门会把同一个角度「广播」到每个比特上——即对 `q[1]` 和 `q[2]` 各施加一次 `ry(theta)`，而不是生成一个两比特门。依据见 [runtime/cudaq/qis/qubit_qis.h:189-197](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L189-L197) 的 `if constexpr (nArgs > 1 && std::is_same_v<mod, base>)` 分支。

**练习 2**：`u3` 需要几个参数？它能表达哪些门？

**参考答案**：三个参数 \((\theta,\phi,\lambda)\)。任意单比特酉门都可以写成 `u3` 的形式（再带上一个全局相位），所以 `h/x/y/z/rx/ry/rz` 都可以被 `u3` 等价表达。定义见 [runtime/cudaq/qis/qubit_qis.h:257-284](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L257-L284)。

### 4.2 ctrl/adj 修饰符与负控（! 操作符）

#### 4.2.1 概念说明

> **重要澄清**：本讲标题与课程大纲里提到「neg 修饰符」，但需要先纠正一个常见误解——**CUDA-Q 的 C++ 源码里并没有一个叫 `cudaq::neg` 的修饰符类型**。修饰符只有三个：`base`、`ctrl`、`adj`。「负控」不是一个新的修饰符，而是通过在量子比特上调用 `negate()`（或写 `!q`）来**给比特打一个「我是负控制」的临时标记**，由门函数在读到这个标记后自动用 `X` 门夹住实现。这一点务必记住，避免去找一个不存在的 `cudaq::neg`。

修饰符的真实定义极其朴素，就是三个空结构体：

[runtime/cudaq/qis/modifiers.h:13-21](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/modifiers.h#L13-L21) —— `base`（默认，普通施加）、`ctrl`（受控）、`adj`（共轭转置）。它们不带任何数据，只用作模板参数「标签」。

负控状态存在量子比特自身里：

[runtime/cudaq/qis/qudit.h:26-28](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qudit.h#L26-L28) —— 每个量子比特内部有一个 `isNegativeControl` 布尔成员，记录它「当前是否被标记为负控」。

#### 4.2.2 核心流程

`op<cudaq::ctrl>(controls..., target)` 的执行流程（以 `oneQubitApply` 为例）：

1. 取出门名 `gateName = QuantumOp::name()`。
2. `static_assert` 校验所有参数都是 2 能级 `qubit`。
3. 把参数打包成 `quditInfos`（比特 id 列表）和 `qubitIsNegated`（每个比特是否被标记为负控）。
4. 若修饰符是 `base`：对每个比特独立施加该门（广播），返回。
5. 否则（`ctrl` 或 `adj`）：**前 \(N-1\) 个比特当控制、最后一个当目标**。
6. 对每个被标记为负控的控制比特，先施加一个 `X`（把「\(c=|0\rangle\) 时触发」翻成「\(c=|1\rangle\) 时触发」）。
7. 调用 `apply(gateName, params, controls, {target}, isAdjoint)`，其中 `isAdjoint = (mod==adj)`。
8. 对刚才夹过的负控比特再施加一次 `X` 还原，并清除其 `isNegativeControl` 标记。

负控的数学等价：在控制比特 \(c\) 上「负控 \(U\)」等价于 \(X_c \cdot C(U) \cdot X_c\)，因为 \(X|0\rangle=|1\rangle\) 把「为 0」翻成「为 1」，作用完再翻回去。

#### 4.2.3 源码精读

修饰符分派的核心函数 `oneQubitApply`：

[runtime/cudaq/qis/qubit_qis.h:63-85](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L63-L85) —— 函数签名与 `base` 广播分支。这段说明：模板参数 `mod` 默认是 `base`；当 `mod==base` 时，门被逐个施加到每一个传入的比特上（广播），不分控制/目标。

受控与负控的处理：

[runtime/cudaq/qis/qubit_qis.h:87-118](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L87-L118) —— `ctrl`/`adj` 分支。这段说明三件事：①前 \(N-1\) 个比特是控制、最后一个是目标；②对每个负控比特用 `X` 门「夹住」（先 `X`、施加门、再 `X`）；③作用完毕后调用 `args.negate()` 把比特的负控标记清掉，使 `!q` 这类写法「用一次即失效」，不影响后续使用。

负控标记的来源——`operator!` 与 `negate()`：

[runtime/cudaq/qis/qudit.h:54-67](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qudit.h#L54-L67) —— `negate()` 翻转 `isNegativeControl` 标记；`operator!()` 是它的语法糖。这段说明：写 `x<cudaq::ctrl>(!q, r)` 时，`!q` 在构造参数时就把 `q` 标成负控，门函数读到该标记后实现「\(q=|0\rangle\) 时翻转 \(r\)」的语义，并在事后自动复位。

> 因此，受控门有三种等价入口：
> - 模板修饰符：`x<cudaq::ctrl>(c, t)`（最通用，支持任意多控制比特）。
> - 语义糖别名：`cnot(c,t)` / `cx(c,t)` / `cy` / `cz` / `ch` / `cs` / `ct`，以及双控的 `ccx(c1,c2,t)`。
> - 受控旋转：`crx/cry/crz/cr1(angle, c, t)`。
>
> 见 [runtime/cudaq/qis/qubit_qis.h:339-364](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L339-L364)。共轭转置的别名 `sdg`/`tdg` 见 [runtime/cudaq/qis/qubit_qis.h:367-368](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L367-L368)，它们内部就是 `s<cudaq::adj>(q)` / `t<cudaq::adj>(q)`。

#### 4.2.4 代码实践

**实践目标**：用负控实现一个「或非」型受控门——只有当控制比特为 \(|0\rangle\) 时才翻转目标，并用采样验证。

**操作步骤**：

1. 写下面的内核（示例代码），分别用正控和负控对照：

   ```cpp
   #include <cudaq.h>

   struct neg_ctrl_demo {
     void operator()(int use_negative) __qpu__ {
       cudaq::qvector q(2);
       x(q[0]);                    // 让控制比特处于 |1>
       if (use_negative)
         x<cudaq::ctrl>(!q[0], q[1]);   // 负控：控制为 |0> 时才翻转
       else
         x<cudaq::ctrl>(q[0], q[1]);    // 正控：控制为 |1> 时才翻转
       mz(q);
     }
   };

   int main() {
     auto pos = cudaq::sample(neg_ctrl_demo{}, 0);
     auto neg = cudaq::sample(neg_ctrl_demo{}, 1);
     printf("positive ctrl: "); pos.dump();
     printf("negative ctrl: "); neg.dump();
     return 0;
   }
   ```

2. 编译运行：`nvq++ neg_ctrl_demo.cpp -o neg_ctrl.x && ./neg_ctrl.x`。

**需要观察的现象**：两次采样的最可能比特串应该不同。

**预期结果**：初始态 \(|10\rangle\)（控制为 1、目标为 0）。正控版会翻转目标得到 \(|11\rangle\)；负控版因控制为 1（不满足「为 0」），目标不翻，得到 \(|10\rangle\)。具体计数「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `x<cudaq::ctrl>(!q, r)` 用过之后，`q` 不会一直保持「负控」状态？

**参考答案**：因为 `oneQubitApply` 在施加完门之后，会遍历参数调用 `args.negate()`，把 `isNegativeControl` 翻回 `false`。见 [runtime/cudaq/qis/qubit_qis.h:104-117](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L104-L117)。这是一种 RAII 式的「自动复位」。

**练习 2**：`x<cudaq::ctrl>(c1, c2, t)` 表示什么门？

**参考答案**：Toffoli（双控非，CCX）。前两个比特是控制、最后一个是目标，仅当 \(c1=c2=|1\rangle\) 时翻转 \(t\)。它也有内置别名 `ccx`，见 [runtime/cudaq/qis/qubit_qis.h:346](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L346)。

### 4.3 apply 算子与控制语义

#### 4.3.1 概念说明

> **再次澄清**：CUDA-Q 的 C++ 用户 API 里**没有一个叫 `cudaq::apply` 的函数**。「apply」在本讲指的是两件实事：
> 1. **底层的 `apply`**——`ExecutionManager::apply(gateName, params, controls, targets, isAdjoint)`，是所有门（无论来自哪种写法）最终汇入的「单点收口」。
> 2. **内核级的控制语义**——`cudaq::control(kernel, ctrl_qubit, args...)` 和 `cudaq::adjoint(kernel, args...)`，它们把**一整个子内核**整体施加控制或取共轭，区别于 4.2 节「单门级」的修饰符。

为什么要区分这两种？因为：

- **单门级修饰符** `op<cudaq::ctrl>(...)` 只控制**一个门**。
- **内核级 `control()`** 控制**一整段子线路**——它对子内核里的每一条门指令都附加上同一个控制比特。这是构造大型受控块（如量子算法里的 `compute_action` 模式）的关键。

#### 4.3.2 核心流程

内核级控制的流程（见 `control()` 实现）：

1. 把控制比特的 id 收集到 `ctrls` 列表。
2. 调用 `getExecutionManager()->startCtrlRegion(ctrls)`，进入「控制区」。
3. 执行子内核 `kernel(args...)`——期间它发出的每条门都会被自动加上这些控制比特。
4. 调用 `endCtrlRegion(ctrls.size())`，退出控制区。

`adjoint()` 同理，用 `startAdjointRegion()` / `endAdjointRegion()` 把区内的每条门取共轭、顺序倒置。

#### 4.3.3 源码精读

底层收口 `apply`：

[runtime/cudaq/qis/execution_manager.h:159-168](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/execution_manager.h#L159-L168) —— `apply` 的纯虚声明。这段说明：无论门是怎么写出来的（修饰符、别名、自定义门、控制区内的门），最终都要变成「门名 + 参数 + 控制比特 + 目标比特 + 是否共轭」这一组数据，交给具体后端模拟器去解释。

单控制比特的 `control()`：

[runtime/cudaq/qis/qubit_qis.h:664-672](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L664-L672) —— 用 `startCtrlRegion`/`endCtrlRegion` 包住子内核调用。这段说明：内核级控制不是「复制粘贴一份带 ctrl 的门」，而是设置一个**区域**，让区域内所有门都带上控制比特。

寄存器（多个控制比特）版与引用列表版：

[runtime/cudaq/qis/qubit_qis.h:674-703](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L674-L703) —— 支持用一个 `qvector`/范围或一个 `std::vector<std::reference_wrapper<qubit>>` 作为多个控制比特。这段说明：`control()` 自然支持多控制，无需嵌套调用。

`adjoint()` 与 `compute_action()`：

[runtime/cudaq/qis/qubit_qis.h:706-737](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L706-L737) —— `adjoint(k,args...)` 把子内核整体取共轭；`compute_action(c,a)` 实现 \(C\,A\,C^{\dagger}\)，`compute_dag_action(c,a)` 实现 \(C^{\dagger}A\,C\)。这段说明：这是化学/组合优化算法里频繁出现的「计算-作用-撤销计算」模式的一等公民支持。

真实示例——`compute_actions.cpp` 用 `x<cudaq::ctrl>` 链构造梯形线路，再展示等价的 `compute_action` 写法：

[docs/sphinx/examples/cpp/other/compute_actions.cpp:32-65](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/other/compute_actions.cpp#L32-L65) —— 左边 `ansatz_handcoded` 用一串 `x<cudaq::ctrl>(q[i], q[i+1])`；右边 `ansatz_compute_action` 用 `cudaq::compute_action(计算块, 作用块)` 表达同一逻辑。这段说明：两种写法等价，后者把「计算块」的撤销交给了框架自动取 adjoint。

#### 4.3.4 代码实践

**实践目标**：体会「单门修饰符」与「内核级 `control()`」的差异——同一个 Toffoli，用两种方式各写一遍。

**操作步骤**：见本讲 **第 5 节「综合实践」**，那里给出了完整可编译的对照程序。本节先做一个最小验证：

1. 写一个 CNOT 子内核，再用 `cudaq::control` 把它包成 Toffoli（示例代码）：

   ```cpp
   #include <cudaq.h>

   struct cnot_k {
     void operator()(cudaq::qubit &c, cudaq::qubit &t) __qpu__ {
       x<cudaq::ctrl>(c, t);      // 一个普通 CNOT
     }
   };

   struct toffoli_via_control {
     void operator()(cudaq::qubit &c1, cudaq::qubit &c2,
                     cudaq::qubit &t) __qpu__ {
       cudaq::control(cnot_k{}, c2, c1, t);  // 把 CNOT 整体控制到 c2 上
     }
   };
   ```

2. 阅读上面的代码：`control(cnot_k{}, c2, c1, t)` 把 `c2` 当控制，对子内核 `cnot_k`（其内部又是 `c1` 控制 `t`）整体施加控制，等价于 `ccx(c1, c2, t)`。

**需要观察的现象**：编译能通过；语义上它就是一个 Toffoli。

**预期结果**：编译通过即说明内核级 `control()` 能正确嵌套在「已经带控制」的子内核上。完整真值表验证放第 5 节，「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`cudaq::control(k, c, args...)` 与 `op<cudaq::ctrl>(c, target)` 的最大区别是什么？

**参考答案**：前者控制**整个子内核**（区域内所有门），后者只控制**单个门**。前者通过 `startCtrlRegion`/`endCtrlRegion` 实现，后者通过把控制比特塞进 `apply` 的 `controls` 参数实现。依据见 [runtime/cudaq/qis/qubit_qis.h:664-672](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L664-L672) 与 [runtime/cudaq/qis/qubit_qis.h:87-101](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L87-L101)。

**练习 2**：`compute_action(C, A)` 展开后是什么？

**参考答案**：\(C\,A\,C^{\dagger}\)——先跑计算块 \(C\)，再跑作用块 \(A\)，最后自动跑 \(C\) 的共轭 \(C^{\dagger}\) 把 borrowed 比特还原。见 [runtime/cudaq/qis/qubit_qis.h:718-725](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L718-L725)。

## 5. 综合实践

**任务**：实现一个 Toffoli 门，分别用「`ctrl` 修饰符」与「`cudaq::control` 内核级控制」两种写法，然后遍历 8 种输入基底态，采样验证真值表 \(t_{\text{out}} = t \oplus (c_1 \cdot c_2)\)。

下面是完整对照程序（示例代码）：

```cpp
#include <cudaq.h>
#include <cstdio>

// 方式 A：ctrl 修饰符（变长控制，前 N-1 个是控制、最后一个是目标）
struct toffoli_modifier {
  void operator()(cudaq::qubit &c1, cudaq::qubit &c2,
                  cudaq::qubit &t) __qpu__ {
    x<cudaq::ctrl>(c1, c2, t);   // 等价于 ccx(c1, c2, t)
  }
};

// 方式 B：内核级 control()，控制一整个 CNOT 子内核
struct cnot_k {
  void operator()(cudaq::qubit &c, cudaq::qubit &t) __qpu__ {
    x<cudaq::ctrl>(c, t);
  }
};
struct toffoli_control_func {
  void operator()(cudaq::qubit &c1, cudaq::qubit &c2,
                  cudaq::qubit &t) __qpu__ {
    cudaq::control(cnot_k{}, c2, c1, t);  // 在 c2 控制下跑 cnot_k(c1, t)
  }
};

// 遍历 8 种输入，先按位制备基底态，再施加 Toffoli，再测量
struct run_truth_table {
  template <typename Kernel>
  void operator()(Kernel &&toffoli, int bits) __qpu__ {
    cudaq::qvector q(3);                    // q[0]=c1, q[1]=c2, q[2]=t
    if (bits & 1) x(q[0]);                  // 设 c1
    if (bits & 2) x(q[1]);                  // 设 c2
    if (bits & 4) x(q[2]);                  // 设 t
    toffoli(q[0], q[1], q[2]);
    mz(q);
  }
};
```

> 说明：`run_truth_table` 用模板参数接收任意一种 Toffoli 实现，体现两种写法**可互换**。如果你的工具链版本对内核模板参数支持有限，可把它拆成两个独立的 `__qpu__` 内核，分别调用 `toffoli_modifier` 与 `toffoli_control_func`。

**操作步骤**：

1. 把上面的代码存为 `toffoli.cpp`（按需把 `run_truth_table` 拆开）。
2. `nvq++ toffoli.cpp -o toffoli.x && ./toffoli.x`。
3. 对 `bits = 0..7` 循环采样，打印每次的最高概率比特串。

**需要观察的现象**：对每个输入 \((c_1,c_2,t)\)，输出应满足 \(t_{\text{out}} = t \oplus (c_1 \wedge c_2)\)。例如输入 `011`（\(c_1=1,c_2=1,t=1\)）应输出 `010`（目标被翻转）；输入 `110`（\(c_1=1,c_2=1,t=0\)）应输出 `111`。

**预期结果**：两种实现（方式 A 与方式 B）的真值表应当**完全一致**，且都匹配 \(t_{\text{out}} = t \oplus (c_1 \cdot c_2)\)。具体采样计数「待本地验证」。

**延伸思考**：把方式 A 里的 `x<cudaq::ctrl>(c1, c2, t)` 换成「带一个负控」的版本 `x<cudaq::ctrl>(!c1, c2, t)`，真值表会怎样变化？先在纸上按 \(X\cdot C(U)\cdot X\) 推一遍，再上机对照。

## 6. 本讲小结

- CUDA-Q 的内建门分两类：无参门（`h/x/y/z/s/t`、`swap`）由 `CUDAQ_QIS_ONE_TARGET_QUBIT_` 宏批量生成；旋转门（`rx/ry/rz/r1`）由 `CUDAQ_QIS_PARAM_ONE_TARGET_` 生成；`u3` 与 `swap` 手写。
- 修饰符只有三个标记类型 `base`/`ctrl`/`adj`，定义在 `modifiers.h`。门函数用模板参数 `mod` 选择解释方式，全部由 `oneQubitApply` / `oneQubitSingleParameterApply` 集中分派。
- **不存在 `cudaq::neg` 修饰符**。负控通过 `operator!` / `qubit.negate()` 给比特打临时标记，门函数读到后用 \(X\cdot C(U)\cdot X\) 实现，并在事后自动复位标记。
- 受控门有三种入口：模板修饰符 `op<cudaq::ctrl>(...)`、内置别名 `cnot/cx/cy/cz/ch/cs/ct/ccx`、受控旋转 `crx/cry/crz/cr1`。共轭别名 `sdg/tdg` 走 `adj`。
- **不存在用户级 `cudaq::apply`**。「apply」指底层 `ExecutionManager::apply`（所有门的单点收口）与内核级 `cudaq::control()` / `cudaq::adjoint()`（控制/共轭一整个子内核，靠 `startCtrlRegion`/`endCtrlRegion` 实现）。
- `compute_action(C,A)` 表达 \(C\,A\,C^{\dagger}\)，是算法里「计算-作用-撤销」模式的一等公民。

## 7. 下一步学习建议

- 下一讲 **u2-l3（测量与采样）** 将讲 `mz/mx/my`、shots 与 `SampleResult` 的读取，与本讲的「门」一起构成「构造线路 → 测量 → 读结果」的完整闭环。
- 想提前理解「门调用如何变成 Quake 指令」的读者，可跳读 [runtime/cudaq/qis/qubit_qis.h:876-941](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L876-L941) 的 `applyQuantumOperation`（自定义门的通用分派器，同样支持负控），这与第 4 单元的 Quake 方言、第 7 单元的自定义门扩展直接相关。
- 想了解自定义门的读者，可先扫一眼注册宏 [runtime/cudaq/qis/qubit_qis.h:958-987](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L958-L987)，它会在 u7-l4「自定义门与自定义算符」中详细讲解。
