# 优化器与梯度

## 1. 本讲目标

本讲把 u3-l3 学到的「求期望值」能力接到「调参」这一环上，回答一个问题：**给定一个参数化量子内核（ansatz）和一个哈密顿量 H，如何自动找到让 ⟨H⟩ 最小的那组参数？**

学完后你应当能够：

- 看懂 CUDA-Q 的 `cudaq::optimizer` 抽象接口，知道 `optimizable_function` 如何把「带梯度」和「不带梯度」两种目标函数统一成一个签名。
- 区分两套后端库（NLopt 与 Ensmallen）各自的算法、是否需要梯度，以及如何配置 `max_eval`、`initial_parameters`、`f_tol` 等旋钮。
- 掌握三种梯度策略（`forward_difference`、`central_difference`、`parameter_shift`）的数学原理、代码实现与评估开销差异。
- 亲手把 `cudaq::observe` + `gradient` + `optimizer` 串成一个完整的变分量子本征求解（VQE）式循环。

## 2. 前置知识

在进入本讲前，请确认你已经理解下列来自前置讲义的概念：

- **变分混合算法的形态（u1-l1）**：量子侧跑参数化线路采样，经典侧计算目标函数与梯度并更新参数，二者低开销交替。本讲的「优化器」就跑在经典侧。
- **`cudaq::observe` 求期望值（u3-l3）**：`cudaq::observe(kernel, H, args...)` 计算 ⟨ψ|H|ψ⟩，返回的 `observe_result` 可经 `operator double()` 直接当成能量值。本讲里它就是优化器的「目标函数」。
- **`spin_op` 代数构造（u3-l3）**：用 `cudaq::spin_op::x/y/z(n)` 配乘法与标量加减即可写出哈密顿量。
- **参数化内核与 `__qpu__`（u1-l4、u2-l4）**：内核可接收 `double` 参数；调用另一个内核时量子比特按引用传递。

一个最小化的经典直觉：所谓「优化」，就是在多维参数空间里沿着「下坡方向」一步一步走，直到走到最低点。「下坡方向」就是负梯度方向，所以**需要梯度**的算法（如 L-BFGS、Adam）每一步都要先估计梯度；而**不需要梯度**的算法（如 COBYLA）则用别的方式（如单纯形、信赖域）来决定下一步走哪儿，通常迭代次数更多但每次更便宜。

## 3. 本讲源码地图

本讲涉及的源码可以分成「对外聚合头」「抽象基类」「具体实现」三层：

| 文件 | 作用 |
| --- | --- |
| [runtime/cudaq/optimizers.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/optimizers.h) | 对外聚合头，仅两行，把 NLopt 与 Ensmallen 两套优化器汇总到 `cudaq::optimizers` 命名空间。 |
| [runtime/cudaq/gradients.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/gradients.h) | 对外聚合头，汇总三种梯度策略到 `cudaq::gradients` 命名空间。 |
| [runtime/cudaq/algorithms/optimizer.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizer.h) | 抽象基类 `cudaq::optimizer`、`optimizable_function` 包装器与 `optimization_result` 类型别名。 |
| [runtime/cudaq/algorithms/gradient.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/gradient.h) | 抽象基类 `cudaq::gradient`，持有 ansatz 仿函数、提供 `getExpectedValue` 与 `compute` 接口。 |
| [runtime/cudaq/algorithms/optimizers/nlopt/nlopt.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizers/nlopt/nlopt.h) / [.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizers/nlopt/nlopt.cpp) | NLopt 后端：`cobyla`、`neldermead`（均不需要梯度）。 |
| [runtime/cudaq/algorithms/optimizers/ensmallen/ensmallen.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizers/ensmallen/ensmallen.h) / [.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizers/ensmallen/ensmallen.cpp) | Ensmallen 后端：`lbfgs`、`adam`、`gradient_descent`、`sgd`（需要梯度）与 `spsa`（不需要）。 |
| [runtime/cudaq/algorithms/gradients/forward_difference.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/gradients/forward_difference.h)、[central_difference.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/gradients/central_difference.h)、[parameter_shift.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/gradients/parameter_shift.h) | 三种梯度策略的完整实现（全部在头文件内）。 |
| [docs/sphinx/examples/cpp/other/gradients.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/other/gradients.cpp) | 官方示例，串起 ansatz + spin_op + 三种优化配置。 |

> 提示：Python 端曾经有 `cudaq.vqe`，但已被移除、迁移到独立的 CUDA-QX 项目（见 [python/cudaq/runtime/vqe.py:L18](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/runtime/vqe.py#L18)，调用即抛 `RuntimeError`）。因此 **C++ 的 `optimizer`/`gradient` API 就是这套机制当前的正典入口**，本讲以 C++ 为主。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先讲优化器本身的接口与配置，再讲梯度策略，最后把二者与 `observe` 串成完整的寻优循环。

### 4.1 优化器接口与配置

#### 4.1.1 概念说明

一个「优化器」要做的事情可以被压缩成一句话：**给我参数维度 `dim` 和一个目标函数 `f(x)`，我把让 `f` 最小的 `(最优值, 最优参数)` 还给你。**

CUDA-Q 把这句话落成两个抽象：

- `optimization_result` 就是 `std::tuple<double, std::vector<double>>`——一个最优值加一组最优参数。
- 目标函数有两种合法写法：要么只返回函数值（无梯度），要么同时填一个梯度向量（有梯度）。CUDA-Q 用 `optimizable_function` 把这两种写法都包装成同一种可调用对象，并用一个布尔位 `_providesGradients` 记住它到底是哪一种。

具体算法被分成两套后端库：

- **NLopt**（`cobyla`、`neldermead`）：**不需要梯度**，适合「测量噪声大、梯度不好估」的场景，但收敛慢、迭代多。
- **Ensmallen**（`lbfgs`、`adam`、`gradient_descent`、`sgd`）：**需要梯度**，收敛快；另有 `spsa` 是 Ensmallen 里唯一不需要梯度的算法（随机扰动估计梯度）。

#### 4.1.2 核心流程

一次 `optimizer.optimize(dim, f)` 的逻辑（以 NLopt 为例）大致是：

```text
1. 检查：f 是否提供梯度  vs  本算法是否需要梯度 → 不匹配就抛 invalid_argument
2. 读取可选配置（未设置则取默认值）：
   - max_eval          默认 INT_MAX
   - initial_parameters 默认全 0 向量
   - lower/upper_bounds 默认 [-π, +π]    ← 注意默认搜索域
   - f_tol              默认 1e-6
3. 配置底层求解器（NLopt / Ensmallen），把 f 注册为最小化目标
4. 循环调用 f(x, grad) → 由求解器决定下一组 x
5. 返回 (最优值, 最优参数)
```

关键点：**目标函数始终被当作「最小化」问题**。如果你想最大化某个量，把它的相反数当目标函数即可。

#### 4.1.3 源码精读

先看顶层的类型与基类。`optimization_result` 是个简单的类型别名，把「最优值 + 最优参数」打包成一个元组：[optimizer.h:L18](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizer.h#L18) 定义 `using optimization_result = std::tuple<double, std::vector<double>>;`。

`optimizable_function` 用模板构造函数 + `static_assert` 在编译期校验传入的可调用对象签名是否合法：[optimizer.h:L36-L54](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizer.h#L36-L54)。如果传入的是 `double(vector<double>)`（无梯度），它就把这个可调用对象包成「忽略第二个参数」的版本，并把 `_providesGradients` 置为 `false`。这一步是后面「签名不匹配就报错」机制的基础。

抽象基类 `cudaq::optimizer` 只有两个纯虚函数：[optimizer.h:L91-L109](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizer.h#L91-L109)。`requiresGradients()` 让子类自报家门，`optimize(dim, optimizable_function&&)` 是统一入口。注意头文件注释里给的用法示例（[optimizer.h:L81-L89](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizer.h#L81-L89)）正是本讲后面要复刻的写法。

NLopt 后端用一个宏批量生成算法类：[nlopt.h:L27-L38](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizers/nlopt/nlopt.h#L27-L38)，目前只实例化出 `cobyla`（不需要梯度）和 `neldermead`（不需要梯度）。它们的公共配置字段集中在 `base_nlopt`：[nlopt.h:L18-L25](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizers/nlopt/nlopt.h#L18-L25)，全是 `std::optional`，意味着「不赋值就用默认」。

`optimize` 的真实实现也在一个宏里（[nlopt.cpp:L31-L98](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizers/nlopt/nlopt.cpp#L31-L98)），有几处值得细看：

- **签名校验**：[nlopt.cpp:L36-L41](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizers/nlopt/nlopt.cpp#L36-L41) 在 optimize 一开头就比对 `providesGradients()` 与 `requiresGradients()`，不匹配立刻抛 `invalid_argument` 并提示正确签名。
- **默认搜索域是 [-π, +π]**：[nlopt.cpp:L43-L50](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizers/nlopt/nlopt.cpp#L43-L50) 写明 `lower_bounds` 默认 `-M_PI`、`upper_bounds` 默认 `+M_PI`。这对量子参数（旋转角）很自然，但若你的参数物理范围不同，务必显式设置边界。
- **对测量噪声友好的报错**：当 NLopt 因舍入误差提前停止（量子采样目标函数本身就带噪声，很容易触发），[nlopt.cpp:L71-L77](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizers/nlopt/nlopt.cpp#L71-L77) 会抛出一条很有指导意义的错误，建议「调小 `max_eval`、调大 `f_tol`、增加测量 shots」。
- 底层算法映射在文件末尾：[nlopt.cpp:L100-L101](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizers/nlopt/nlopt.cpp#L100-L101) 把 `cobyla` 映射到 `LN_COBYLA`、`neldermead` 映射到 `LN_NELDERMEAD`（前缀 `LN_` 表示 local/no-derivative）。

Ensmallen 后端的类层级结构类似：`BaseEnsmallen` 收集公共字段（[ensmallen.h:L17-L28](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizers/ensmallen/ensmallen.h#L17-L28)），再用宏生成具体算法。注意宏的第二个参数就是「是否需要梯度」：`lbfgs` 是 `true`（[ensmallen.h:L40-L41](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizers/ensmallen/ensmallen.h#L40-L41)），`spsa` 是 `false`（[ensmallen.h:L42-L44](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizers/ensmallen/ensmallen.h#L42-L44)），`adam`/`gradient_descent`/`sgd` 都是 `true`。

Ensmallen 与 CUDA-Q 之间靠一个 `FunctionAdaptor` 桥接：[ensmallen.cpp:L20-L54](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizers/ensmallen/ensmallen.cpp#L20-L54) 把 `cudaq::optimizable_function` 适配成 ensmallen 要求的 `Evaluate` / `EvaluateWithGradient` 接口（内部用 armadillo 矩阵 `arma::mat` 与 `std::vector<double>` 互转）。`lbfgs::optimize` 本体在 [ensmallen.cpp:L66-L86](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizers/ensmallen/ensmallen.cpp#L66-L86)，可以看到默认 `step_size=1e-2`、默认 `max_eval` 为 `size_t` 上限，并把可选的 `max_line_search_trials` 透传给 `ens::L_BFGS`。`validate` 函数 [ensmallen.cpp:L58-L64](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizers/ensmallen/ensmallen.cpp#L58-L64) 与 NLopt 那段做一样的签名校验。

#### 4.1.4 代码实践

**实践目标**：体会「无梯度优化器」的最小用法，并观察默认搜索域与迭代次数。

**操作步骤**：

1. 复制官方示例 `docs/sphinx/examples/cpp/other/gradients.cpp` 的前半段，只保留 COBYLA 那一段（见后面 4.3 的源码），删掉两段带梯度的部分。
2. 编译运行：`nvq++ gradients.cpp -o gs.x && ./gs.x`（与示例文件首部注释 [gradients.cpp:L9-L12](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/other/gradients.cpp#L9-L12) 一致）。
3. 试着给 `optimizer` 加上约束：`optimizer.max_eval = 30;`，再跑一次。

**需要观察的现象**：程序会逐次打印 `<H>(x0, x1) = e`，能看到参数在 [-π, π] 范围内被反复试探；COBYLA 会迭代很多次（示例注释 [gradients.cpp:L43-L45](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/other/gradients.cpp#L43-L45) 提示「Should see many more iterations」）。

**预期结果**：能量从初始点逐步下降，最终收敛到 deuteron N=3 基态能量附近（约 -2.045 量级）。设了 `max_eval=30` 后会在迭代 30 次时停下，能量可能尚未收敛。

**待本地验证**：精确的迭代步数与最终能量值依赖本机随机性（`observe` 默认带采样噪声），请以本地实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：如果对一个 `requiresGradients()` 为 `true` 的优化器（如 `lbfgs`）传入只返回函数值、不填梯度的目标函数，会发生什么？

**答案**：`optimize` 在 [nlopt.cpp:L36-L41](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizers/nlopt/nlopt.cpp#L36-L41)（Ensmallen 在 [ensmallen.cpp:L58-L64](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizers/ensmallen/ensmallen.cpp#L58-L64)）一开头就会检测到 `providesGradients()==false` 与 `requiresGradients()==true` 不匹配，抛出 `std::invalid_argument`，提示你改用带 `std::vector<double>& grad_x` 的签名。

**练习 2**：默认搜索域是多少？为什么这个选择对量子参数很自然？

**答案**：默认 `lower_bounds=-π`、`upper_bounds=+π`（[nlopt.cpp:L48-L50](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizers/nlopt/nlopt.cpp#L48-L50)）。因为量子内核里的参数几乎都是旋转角 `rx/ry/rz(θ)`，而旋转角以 2π 为周期，[-π, π] 恰好覆盖一个完整周期，足以表达任意单参数酉变换。

---

### 4.2 梯度策略

#### 4.2.1 概念说明

当优化器需要梯度时，你必须告诉它「在当前参数点 x 处，目标函数对每个参数的偏导是多少」。在量子场景里，目标函数 ⟨H(x)⟩ 没有解析表达式可直接求导（它要靠 `observe` 测出来），所以我们只能**用「在 x 附近多测几个点」来估计梯度**——这就是「梯度策略」干的事。

CUDA-Q 提供三种策略，全是 `cudaq::gradient` 的子类：

- `forward_difference`（前向差分）：每个参数只多估 1 个点，最便宜但精度最低。
- `central_difference`（中心差分）：每个参数多估 2 个点，精度更高、对称抵消一阶误差。
- `parameter_shift`（参数平移）：每个参数多估 2 个点，但**位移量是 π 的倍数**，对「Pauli 旋转门生成的线路」是**解析精确**的，不引入截断误差——这是量子计算特有的优势。

#### 4.2.2 核心流程

三种策略在结构上高度一致，差别只在「往哪挪、挪多少、怎么算」：

```text
对每个参数 i = 0..N-1：
   复制 x 得 tmpX
   计算 px = ⟨H⟩(tmpX 在第 i 维扰动后)
   [central/parameter_shift 还要] 计算 mx = ⟨H⟩(tmpX 在第 i 维反向扰动后)
   用各自公式写入 dx[i]
   把 tmpX[i] 复位回 x[i]
```

数学上：

- 前向差分（步长 `h`，复用已算好的 `f(x)`）：

\[
\frac{\partial f}{\partial x_i} \approx \frac{f(x_i + h) - f(x_i)}{h}
\]

- 中心差分（步长 `h`）：

\[
\frac{\partial f}{\partial x_i} \approx \frac{f(x_i + h) - f(x_i - h)}{2h}
\]

- 参数平移（默认 `shiftScalar = 0.5`，即位移 `π/2`）：

\[
\frac{\partial f}{\partial x_i} = \frac{f(x_i + \tfrac{\pi}{2}) - f(x_i - \tfrac{\pi}{2})}{2}
\]

最后一个式子对「参数以 \(e^{-\mathrm{i}\theta P/2}\) 形式进入线路、生成元 \(P/2\) 特征值为 ±1/2 的 Pauli 旋转」是**精确等式**而非近似——这正是 parameter-shift 在变分量子算法中地位特殊的根本原因。它和中心差分长得像，但分母是 2 而非 \(2h\)，因为这是离散采样下的精确值。

**评估开销对比**（N 为参数个数，每次 `⟨H⟩` 估计 = 一次 `observe`，而 `observe` 内部又要逐项测量 spin_op）：

| 策略 | 每步额外 `observe` 次数 | 是否精确 | 典型步长/位移 |
| --- | --- | --- | --- |
| `forward_difference` | N（复用 `funcAtX`） | 否（O(h) 误差） | `h=1e-4` |
| `central_difference` | 2N | 否（O(h²) 误差） | `h=1e-4` |
| `parameter_shift` | 2N | 是（对 Pauli 旋转） | 位移 `shiftScalar·π`，默认 0.5 |

#### 4.2.3 源码精读

抽象基类 `cudaq::gradient`（[gradient.h:L36-L148](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/gradient.h#L36-L148)）的核心职责有二：

1. **把「任意签名的内核」统一成 `void(vector<double>)` 的 ansatz 仿函数** `ansatz_functor`。当内核签名不是标准的「单个 `vector<double>`」时（本讲例子里 `deuteron_n3_ansatz` 接收两个 `double`），构造时额外传一个 **Argument Mapper**，把参数向量拆解成内核真正想要的实参元组：[gradient.h:L68-L74](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/gradient.h#L68-L74) 与 [gradient.h:L124-L130](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/gradient.h#L124-L130)。
2. **提供「在给定参数处算 ⟨H⟩」的能力** `getExpectedValue`：[gradient.h:L47-L49](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/gradient.h#L47-L49) 直接调 `cudaq::observe(ansatz_functor, h, x)`。这一行就是「梯度策略」与 u3-l3「observe」之间的接口——所有梯度估算最终都落到对 `observe` 的反复调用上。

子类必须实现两个 `compute` 重载（[gradient.h:L134-L142](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/gradient.h#L134-L142)）：一个针对「spin_op 哈密顿量」（用 `getExpectedValue`），一个针对「任意用户函数 `func`」（直接调 `func(tmpX)`）；以及一个 `clone()`（[gradient.h:L145](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/gradient.h#L145)，供并行场景拷贝策略对象）。

`parameter_shift` 的 spin_op 版 `compute`：[parameter_shift.h:L25-L39](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/gradients/parameter_shift.h#L25-L39)。注意三处扰动顺序：先 `+shiftScalar*π` 算 `px`，再 `-2*shiftScalar*π`（相对 `px` 点后退两步）算 `mx`，最后 `+shiftScalar*π` 把 `tmpX[i]` 复位回原值，写入 `dx[i]=(px-mx)/2`。默认 `shiftScalar=0.5` 定义在 [parameter_shift.h:L17](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/gradients/parameter_shift.h#L17)。任意函数版在 [parameter_shift.h:L43-L61](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/gradients/parameter_shift.h#L43-L61)，结构完全镜像。

`central_difference`：[central_difference.h:L26-L40](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/gradients/central_difference.h#L26-L40)，公式 `dx[i]=(px-mx)/(2*step)`，默认 `step=1e-4`（[central_difference.h:L18](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/gradients/central_difference.h#L18)）。

`forward_difference` 是唯一**复用已算函数值**的策略：[forward_difference.h:L29-L40](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/gradients/forward_difference.h#L29-L40)。它的 `compute` 第四个参数叫 `funcAtX`（不是 `exp_h`），公式 `dx[i]=(px-funcAtX)/step`，因为 `funcAtX` 就是当前点 x 处的 ⟨H⟩，由调用方（优化循环）已经算过一次，这里只再补 `+step` 一个点即可，所以每参数只需 1 次额外 `observe`。

#### 4.2.4 代码实践

**实践目标**：用「源码阅读 + 手算」确认 parameter-shift 的默认位移与公式，理解它为何精确。

**操作步骤**：

1. 打开 [parameter_shift.h:L25-L39](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/gradients/parameter_shift.h#L25-L39)，把 `shiftScalar` 代入默认值 `0.5`，写出位移量与最终 `dx[i]` 表达式。
2. 对照 [central_difference.h:L26-L40](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/gradients/central_difference.h#L26-L40)，找出两者**代码结构相同**但**分母不同**的那一行（一个是 `/2.`，一个是 `/(2.*step)`）。
3. 思考：若把 `parameter_shift` 的 `shiftScalar` 改成示例里用的 `1e-1`（见 4.3），公式 `(px-mx)/2` 还精确吗？

**需要观察的现象**：parameter-shift 默认位移是 `π/2`；中心差分位移是 `1e-4`。两者代码骨架几乎一样，差别只在「位移尺度」和「分母」。

**预期结果**：手算得到 parameter-shift 默认下 `dx[i] = (⟨H⟩(x_i+π/2) - ⟨H⟩(x_i-π/2))/2`，对 Pauli 旋转精确成立；`shiftScalar=0.1` 时位移变成 `0.1π`，公式分母仍是 2，此时它**不再是精确的参数平移规则**，而退化成一个步长较大的近似——这解释了为什么示例作者能随手调它。

**待本地验证**：可在 4.3 的程序里分别用 `shiftScalar=0.5` 与 `0.1` 跑同一 ansatz，比较收敛能量是否一致。

#### 4.2.5 小练习与答案

**练习 1**：三种策略里，哪个最省 `observe` 调用？为什么？

**答案**：`forward_difference` 最省。因为它复用了调用方已经算好的当前点函数值 `funcAtX`（[forward_difference.h:L38](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/gradients/forward_difference.h#L38)），每个参数只需再多估 1 个点，共 N 次；而 `central_difference` 与 `parameter_shift` 都需要 2N 次。

**练习 2**：为什么参数平移对量子线路「精确」，而中心差分只是「近似」？

**答案**：当参数以 Pauli 旋转 \(e^{-\mathrm{i}\theta P/2}\) 进入线路时，期望值对该参数呈严格的正弦/余弦依赖，数学上可证 \(\partial_\theta f = [f(\theta+\pi/2)-f(\theta-\pi/2)]/2\) 是恒等式。中心差分则是泰勒展开截断，永远带 O(h²) 误差——它适用于「目标函数没有这种特殊结构」的一般情况。

---

### 4.3 observe + gradient 寻优

#### 4.3.1 概念说明

把前两个模块拼起来：优化器每迭代一步，都会回调你给的目标函数；在这个回调里，你要做两件事——

1. 调 `cudaq::observe(ansatz, H, params)` 算出当前能量 `e`（这是优化器要的最小化目标）；
2. 若该优化器需要梯度，再调 `gradient.compute(x, grad_vec, H, e)` 把梯度填进 `grad_vec`（`compute` 内部会自动再多调几次 `observe`）。

这套「observe + gradient + optimizer」的组合就是 VQE 的经典骨架。注意一个微妙点：**梯度对象自己持有一份 ansatz 仿函数**（构造时传入），而 `observe` 又在目标函数里被你直接调用一次——也就是说，同一参数点处 `observe` 至少被调用 1 次（算 `e`），`compute` 再追加 2N 或 N 次。两者用的是同一个 ansatz、同一个 H，结果一致。

#### 4.3.2 核心流程

```text
构造 ansatz 内核（__qpu__，参数化）
构造哈密顿量 H（spin_op 代数）
[若需要梯度] 用 (kernel, argsMapper) 构造 gradient 对象
构造 optimizer 对象，设置 max_eval / initial_parameters / ... 选项
调用 optimizer.optimize(dim, [&](x, grad_vec){
    e = cudaq::observe(kernel, H, x[0], x[1], ...);   // 1 次期望值
    if (优化器需要梯度) gradient.compute(x, grad_vec, H, e);  // 内部多次期望值
    记录/打印 (x, e);
    return e;
})  → 返回 (最优能量, 最优参数)
```

其中 `argsMapper` 的作用是把优化器手里的 `vector<double>` 翻译成内核真正要的实参元组——本例内核接收两个 `double`，所以 mapper 是 `[](vector<double> x){ return make_tuple(x[0], x[1]); }`。

#### 4.3.3 源码精读

官方示例 [gradients.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/other/gradients.cpp) 正是这套骨架的范本。

ansatz 内核接收两个旋转角 `x0, x1`，作用于 3 个比特：[gradients.cpp:L19-L31](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/other/gradients.cpp#L19-L31)。哈密顿量 `h3` 是 deuteron（氘核）N=3 截断下的哈密顿量，用 `spin_op` 代数直接写出来：[gradients.cpp:L35-L41](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/other/gradients.cpp#L35-L41)。

**第一段：无梯度优化（COBYLA）**：[gradients.cpp:L45-L52](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/other/gradients.cpp#L45-L52)。目标函数 lambda 只算 `e` 并返回，不碰 `grad_vec`——这正对应「无梯度签名」，`optimizable_function` 会把 `_providesGradients` 置为 `false`，与 `cobyla` 的 `requiresGradients()==false` 匹配。

Argument Mapper 把 `vector<double>` 拆成两个 `double`：[gradients.cpp:L54-L56](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/other/gradients.cpp#L54-L56)。

**第二段：参数平移 + L-BFGS**：[gradients.cpp:L60-L71](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/other/gradients.cpp#L60-L71)。这里把 `shiftScalar` 从默认 `0.5` 改成 `1e-1`（[gradients.cpp:L62](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/other/gradients.cpp#L62)），并把 `lbfgs.max_line_search_trials` 设为 10（[gradients.cpp:L64](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/other/gradients.cpp#L64)）。目标函数 lambda 现在先 `observe` 算 `e`，再 `gradient.compute(x, grad_vec, h3, e)` 填梯度，最后返回 `e`。注释提示「Should see fewer iterations」——带梯度后 L-BFGS 用更少迭代收敛。

**第三段：中心差分 + L-BFGS，并演示改选项**：[gradients.cpp:L73-L84](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/other/gradients.cpp#L73-L84)。换成 `central_difference` 策略，并设置 `initial_parameters={.1,.1}` 与 `max_eval=10`。注意 `optimizer_lbfgs` 对象被复用——`initial_parameters` 与 `max_eval` 是其成员，重新赋值后影响下一次 `optimize`。

最终结果用结构化绑定接收：`auto [opt_val, opt_params] = optimizer.optimize(...)`，正是 `optimization_result` 这个 `tuple<double, vector<double>>` 的解包。

#### 4.3.4 代码实践（本讲主任务）

**实践目标**：用优化器 + 梯度对一个参数化 ansatz 做最小化，记录每步能量，对比两种梯度策略的收敛曲线。

**操作步骤**：

1. 直接用官方示例作为基底：`cp docs/sphinx/examples/cpp/other/gradients.cpp ./my_gradients.cpp`。
2. 编译运行：`nvq++ my_gradients.cpp -o gs.x && ./gs.x | tee run.log`。这会一次性跑完三段（COBYLA、parameter_shift+L-BFGS、central_difference+L-BFGS）。
3. 把每段打印的 `<H>(x0, x1) = e` 行分别抽出来，按打印顺序当成「迭代步 k → 能量 e」的数据点。可以用下面的最小脚本（示例代码，非项目原有）提取第二、第三段（带梯度的两段）的能量序列：

   ```python
   # 示例代码：抽取 my_gradients 输出里每段 <H> 行的能量，画收敛曲线
   import re, matplotlib.pyplot as plt
   lines = open("run.log").read().splitlines()
   segs, cur = [], []
   for ln in lines:
       m = re.search(r"<H>\([-\d.]+, [-\d.]+\) = ([-\d.]+)", ln)
       if m:
           cur.append(float(m.group(1)))
       elif cur:
           segs.append(cur); cur = []
   if cur: segs.append(cur)
   for i, s in enumerate(segs):
       plt.plot(range(len(s)), s, marker=".", label=f"segment {i}")
   plt.xlabel("iteration k"); plt.ylabel("<H>"); plt.legend(); plt.savefig("conv.png")
   ```

4. 对比 segment 1（parameter_shift）与 segment 2（central_difference）的收敛曲线，看哪个更快、哪个更稳。
5. 进阶：把第二段的 `shiftScalar` 改回默认 `0.5`（删掉 [gradients.cpp:L62](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/other/gradients.cpp#L62) 那行），重跑，观察收敛能量是否更接近第三段。

**需要观察的现象**：

- COBYLA 段（segment 0）迭代次数明显多于两段带梯度的。
- parameter_shift 与 central_difference 都能把能量压到接近的极小值，但迭代次数与轨迹不同。
- `max_eval=10` 会强制第三段在第 10 次评估后停下，能量可能尚未到最低（输出行 `Opt loop found ... at [...]`）。

**预期结果**：三段最终能量都落在 deuteron N=3 基态（约 -2.045）附近；带梯度的两段迭代次数显著少于 COBYLA。

**待本地验证**：精确能量值、迭代步数、曲线形状取决于本地 `observe` 的采样噪声与机器精度，请以本地 `run.log` 为准；上面的 Python 片段需要本机安装 `matplotlib`，若没有可改为手动记录关键点。

#### 4.3.5 小练习与答案

**练习 1**：在第二段目标函数里，`cudaq::observe` 和 `gradient.compute` 各自会触发多少次期望值评估？（设参数数为 2、策略为 parameter_shift）

**答案**：`observe` 显式调用 1 次（算 `e`，[gradients.cpp:L67](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/other/gradients.cpp#L67)）。`gradient.compute` 内部对每个参数做 2 次扰动评估（[parameter_shift.h:L31-L34](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/gradients/parameter_shift.h#L31-L34)），2 个参数共 4 次。所以单次目标函数回调共约 5 次 `observe`（每次 `observe` 内部还要按 spin_op 项数逐项测量）。

**练习 2**：为什么示例里 `optimizer_lbfgs` 对象能在第二、第三段之间复用？复用时哪些字段会被新赋值覆盖？

**答案**：因为 `lbfgs` 的配置字段都是它的成员变量（继承自 `BaseEnsmallen`，[ensmallen.h:L17-L28](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizers/ensmallen/ensmallen.h#L17-L28)）。第三段在 [gradients.cpp:L76-L77](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/other/gradients.cpp#L76-L77) 重新赋值了 `initial_parameters` 与 `max_eval`，覆盖了之前的状态；`max_line_search_trials=10` 仍保留。注意 `optimize` 内部 `initial_parameters.value_or(...)` 每次都重新读取这些成员（[ensmallen.cpp:L71](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizers/ensmallen/ensmallen.cpp#L71)），所以复用是安全的。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个「**对比实验**」：

**任务**：用同一个 `deuteron_n3_ansatz` 与同一个 `h3`，跑四组配置，把它们在第 k 次目标函数调用时的能量画在同一张图上：

1. `cobyla`（无梯度）
2. `lbfgs` + `forward_difference`
3. `lbfgs` + `central_difference`
4. `lbfgs` + `parameter_shift`（默认 `shiftScalar=0.5`）

**要求**：

- 每组都用相同的 `initial_parameters`（例如 `{0.1, 0.1}`）和相同的 `max_eval`（例如 50），保证可比。
- 在目标函数 lambda 里追加一行 `printf`，打印 `k` 与 `e`，便于事后画曲线。
- 写一段话总结：哪种策略收敛最快（按目标函数调用次数计，而非迭代次数）？哪种最稳（最终能量最低/波动最小）？这是「评估开销」与「精度」之间的权衡。

**提示**：

- 四组里只有 `cobyla` 的目标函数不带梯度参数；其余三组的 lambda 形如第二段那样先 `observe` 再 `compute`。
- 想让对比更干净，可以在每组跑之前把 `optimizer` 与 `gradient` 对象都重新构造一次，避免字段互相污染。
- 若本机没有 `matplotlib`，把四组 `(k, e)` 序列各自写进一个 `.csv`，用任意工具画图即可。

**预期结论**（待本地验证）：按目标函数调用次数计，带梯度的三组通常比 COBYLA 更快到达基态附近；`forward_difference` 单步最便宜但噪声敏感、可能震荡；`central_difference` 与 `parameter_shift` 都较稳，后者在默认位移下对这条 Pauli 旋转线路精确，理论上更可信。

## 6. 本讲小结

- CUDA-Q 的优化器统一抽象在 `cudaq::optimizer`（[optimizer.h:L91-L109](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/optimizer.h#L91-L109)）：`optimize(dim, f)` 返回 `(最优值, 最优参数)`，`requiresGradients()` 决定它要不要梯度。`optimizable_function` 把「带/不带梯度」两种目标函数签名统一成一种可调用对象，并用 `_providesGradients` 标记。
- 两套后端：NLopt 给出 `cobyla`、`neldermead`（无梯度，默认搜索域 [-π, π]）；Ensmallen 给出 `lbfgs`、`adam`、`gradient_descent`、`sgd`（需梯度）与 `spsa`（无梯度）。两者都会在签名不匹配时抛 `invalid_argument`。
- 三种梯度策略都继承 `cudaq::gradient`，本质都是「在当前点附近多估几次 ⟨H⟩」：`forward_difference` 复用当前点（每参数 1 次）、`central_difference`（每参数 2 次，O(h²) 近似）、`parameter_shift`（每参数 2 次，对 Pauli 旋转精确）。所有估算最终都通过基类的 `getExpectedValue` 落到 `cudaq::observe`（[gradient.h:L47-L49](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/gradient.h#L47-L49)）。
- 当内核签名不是单个 `vector<double>` 时，构造梯度对象要额外传一个 Argument Mapper，把参数向量拆成内核真正想要的实参元组（[gradient.h:L68-L74](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/gradient.h#L68-L74)）。
- VQE 骨架 = 在 `optimizer.optimize` 的目标函数 lambda 里：先 `cudaq::observe` 算能量并返回，再（若需要）`gradient.compute` 填梯度。官方示例 [gradients.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/other/gradients.cpp) 用同一段 ansatz 演示了 COBYLA、parameter_shift+L-BFGS、central_difference+L-BFGS 三种配置。
- Python 端的 `cudaq.vqe` 已被移除并迁移到 CUDA-QX（[vqe.py:L18](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/runtime/vqe.py#L18)）；当前这套 `optimizer`/`gradient` C++ 接口是变分寻优的正典入口。

## 7. 下一步学习建议

- **想更深入算符与哈密顿量构造**：进入 u3-l5《算符代数：spin/fermion/boson/matrix》，学会用 `fermion_op` 构造真实分子哈密顿量再映射成 `spin_op`，那样喂给本讲的优化器就是真正的化学 VQE。
- **想看状态向量层面的精确计算**：阅读 u3-l6《get_state、酉矩阵与状态获取》，了解如何绕过采样、直接拿到态向量，从而脱离 `observe` 的采样噪声来验证梯度公式。
- **想理解 `observe` 内部如何逐项测量**：回顾 u3-l3 的 `measureSpinOp` 与 `canHandleObserve` 能力位，它会决定本讲里每一次 `observe` 到底是「矩阵直算」还是「逐项采样」，直接影响优化收敛的噪声水平。
- **想动手扩展**：参考 [gradient.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/gradient.h) 里子类的写法，尝试实现一个「同时平移」的梯度策略（如 SPSA 式随机扰动），只需继承 `cudaq::gradient` 并实现两个 `compute` 与 `clone`。
