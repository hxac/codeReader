# _precompute/：参考数据与系数的生成脚本

## 1. 本讲目标

学完本讲后，读者应该能够：

- 说清楚 `scipy/special/_precompute/` 这个目录存在的根本理由——数值库需要「可信参考数据」，而有些数据要么推导极繁、要么计算极慢，只能在开发阶段离线算好并「固化」进仓库。
- 区分两类预计算脚本：「系数生成型」（产出 C 头文件常量或打印到屏幕供手工拷贝）与「参考数据型」（产出 `(输入, 期望值)` 网格文本，最终打包成 `.npz` 供 `FuncData` 测试消费）。
- 读懂 `utils.py` 的 `lagrange_inversion`、`gammainc_asy.py` 的 Temme 渐近系数、`gammainc_data.py` 的 mpmath 数据生成等典型脚本，并理解「为什么要用高精度（`workdps`）算、再截断到 17 位」的工程动机。
- 在 `meson.build` 与 `tests/test_precompute_*.py` 中找到这些脚本的「安装入口」与「测试入口」，并解释为什么脚本本身**不**在构建期被运行。

本讲承接 u9-l1（`FuncData`／数据驱动测试）与 u9-l2（mpmath 高精度参考验证），回答一个上游问题：那些测试用的 `.npz` 数据、以及内核里写死的渐近展开系数，**最初是从哪里来的**。

## 2. 前置知识

- **特殊函数的渐近展开（asymptotic expansion）**：很多特殊函数在大自变量下没有简单的初等表达，但有形如 \(f(x) \sim e^{g(x)}\sum_{k} c_k / x^k\) 的级数。系数 \(c_k\) 往往由递推关系给出，手算极易出错，于是交给计算机用高精度代数系统一次性算出。
- **mpmath 与 sympy**：`mpmath` 是任意精度浮点库（u9-l2 已用作「黄金参考」），`sympy` 是符号计算库。本目录大量出现 `with mp.workdps(50):` 这样的精度上下文——它把当前工作的十进制有效位数临时提到 50 位乃至 1000 位。
- **Lagrange 反演（Lagrange inversion）**：若级数 \(f(x)=a_1 x+a_2 x^2+\cdots\) 满足 \(a_0=0\)，可求其「反函数级数」\(g(x)\) 使 \(f(g(x))=x\)。本目录用它从某个辅助函数的 Taylor 系数反演出渐近展开系数。
- **Padé 逼近（Padé approximant）**：用有理函数 \(p(x)/q(x)\) 去逼近一个幂级数，常比截断 Taylor 级数收敛域更宽。
- **mpf 与 double 的转换陷阱**：高精度 `mpf` 直接 `float()` 会踩到二进制舍入的坑（如 `float(mpf("0.99999999999999999"))` 会少一位），因此本目录统一用 `_mptestutils.mpf2float` 走 `nstr(x, 17)` 再 `float`。

## 3. 本讲源码地图

本讲聚焦 `scipy/special/_precompute/` 目录，并牵涉其产物的消费端：

| 文件 | 角色 |
| --- | --- |
| [`_precompute/meson.build`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/meson.build#L1-L22) | 把 13 个脚本作为纯 Python 源**安装**进包（仅安装，不运行） |
| [`_precompute/utils.py`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/utils.py#L1-L38) | 公共工具，目前只有 `lagrange_inversion`（级数反演） |
| [`_precompute/gammainc_asy.py`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/gammainc_asy.py#L1-L117) | **系数生成型**：非完整 Gamma 函数的 Temme 渐近系数 \(d_{k,n}\)，写入 C 头文件 |
| [`_precompute/expn_asy.py`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/expn_asy.py#L1-L54) | **系数生成型**：广义指数积分渐近多项式 \(A_k(x)\) |
| [`_precompute/loggamma.py`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/loggamma.py#L1-L43) / [`zetac.py`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/zetac.py#L1-L27) | **系数生成型**：log-Gamma 的 Stirling/Taylor 系数、ζ(x)−1 的 Taylor 系数，打印到屏幕 |
| [`_precompute/wright_bessel.py`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/wright_bessel.py#L1-L343) | **系数生成型**（符号）：Wright 广义 Bessel 函数各级数展开的符号推导，产出代码片段字符串 |
| [`_precompute/gammainc_data.py`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/gammainc_data.py#L1-L124) | **参考数据型**：大参数下 `gammainc/gammaincc` 的 mpmath 参考值网格 |
| [`_precompute/wright_bessel_data.py`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/wright_bessel_data.py#L1-L152) | **参考数据型**：Wright Bessel 函数的三维网格参考值 |
| [`_precompute/hyp2f1_data.py`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/hyp2f1_data.py#L1-L485) | **回归分析型**：scipy 与 mpmath 的 `hyp2f1` 大规模比对，产出可直接粘贴的 `pytest.param` 测试用例 |
| [`_precompute/cosine_cdf.py`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/cosine_cdf.py#L1-L18) / [`lambertw.py`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/lambertw.py#L1-L68) / [`wrightomega.py`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/wrightomega.py#L1-L41) | 小型系数/误差分析脚本（Padé、分支级数、误差阈值验证） |

消费端（产物去向）：

| 文件 | 角色 |
| --- | --- |
| [`_mptestutils.py` (mpf2float)](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_mptestutils.py#L308-L317) | 数据脚本用来安全把 `mpf` 折回 `double` |
| [`tests/data/local/*.txt`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/data/local) | 参考数据脚本写出的文本（`gammainc.txt`、`gammaincc.txt`、`wright_bessel.txt`） |
| [`meson.build` (npz 规则)](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L175-L208) | 构建期把 `tests/data/local/` 打包成 `local.npz` |
| [`tests/test_data.py`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_data.py#L54-L56) | 加载 `local.npz`，用 `data_local()` 喂给 `FuncData` |
| [`tests/test_precompute_gammainc.py`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_precompute_gammainc.py#L1-L113) / [`test_precompute_expn_asy.py`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_precompute_expn_asy.py#L1-L25) / [`test_precompute_utils.py`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_precompute_utils.py#L1-L37) | 对脚本里的**纯计算函数**做单元测试（不跑 `main()`） |

## 4. 核心概念与源码讲解

### 4.1 离线预计算的动机与两种工作模式

#### 4.1.1 概念说明：为什么需要离线预计算

`scipy.special` 是一个数值库。数值库的「正确性」最终只能靠**与可信参考值比对**来保证（见 u9-l1 的 `FuncData`、u9-l2 的 mpmath 比对）。但有两类东西**没法在每次测试时现算**：

1. **内核里写死的级数系数**。例如非完整 Gamma 函数 \(\Gamma(a,x)\) 在大参数下用 Temme 渐近展开，需要一张 \(25\times 25\) 的系数表 \(d_{k,n}\)。这些系数由层层递推得到，手算不现实；若在运行时现算，既要拖慢启动、又要引入对 mpmath/sympy 的强依赖。
2. **大参数下的参考数据**。u9-l2 的 `test_mpmath.py` 会现算 mpmath 作参考，但对 `gammainc(1e14, 1e14)` 这种点，mpmath 一次要算十几分钟、甚至不收敛（`maxterms` 不够），根本塞不进常规测试。

解决办法是**开发阶段离线算一次、把结果以文本/常量形式提交进仓库**，让运行时和测试直接读取固化好的结果。`_precompute/` 就是这些「一次性算数脚本」的集中安置点。它们是**开发工具（developer tooling），不是运行时依赖**——这就是为什么 `meson.build` 只「安装」它们、从不在构建期执行它们（见 4.4）。

#### 4.1.2 核心流程：两种产物模式

通览 13 个脚本，可归为两种工作模式：

```text
模式 A：系数生成型（gammainc_asy / expn_asy / loggamma / zetac / wright_bessel / cosine_cdf / lambertw）
  读数学文献(DLMF) → 用 mpmath/sympy 高精度推导系数 → 截断到 17~18 位有效数字
   ├─ 写入 C 头文件常量（gammainc_asy: ../cephes/igam.h），或
   └─ print 到屏幕，由开发者手工拷进内核源码

模式 B：参考数据型（gammainc_data / wright_bessel_data）
  在参数网格上采样 (a, x, ...) → 用 mpmath 高精度算期望值 → mpf2float 折回 double
   └─ np.savetxt 到 tests/data/local/*.txt → 构建期打包成 local.npz → test_data.py 消费

变体 C：回归分析型（hyp2f1_data）
  在大规模参数×复平面网格上同时算 scipy 与 mpmath → 输出 TSV + 直接生成 pytest.param 字符串
```

两种模式共享一套工程套路，几乎在每个文件顶部都能看到：

- `try: import mpmath as mp except ImportError: pass`——软依赖，缺库时脚本静默不可用。
- `with mp.workdps(N):`——临时提升精度算关键系数。
- 自动生成产物的头部警告，例如 `gammainc_asy.py` 写出的头文件以 `/* This file was automatically generated ... Do not edit it manually! */` 开头。
- **原子写**：先写 `igam.h.new`，再 `os.rename` 覆盖，避免中途崩溃留下半个文件（见 4.2.3）。
- 统一入口 `if __name__ == "__main__": main()`。

### 4.2 高精度系数生成：utils.py 与几个典型脚本

#### 4.2.1 概念说明：为什么「高精度算、再截断」

设某系数的真值为 \(c\)，运行时内核以双精度 \(c_{\text{dbl}}\) 使用它。若我们只用双精度去**算** \(c\)，递推过程中的舍入误差会层层累积，最终 \(c_{\text{dbl}}\) 可能连一位都不准。正确做法是：用远高于双精度的位数（50–100 位十进制）算出 \(c\) 的近似 \(\hat c\)，再**截断**到 17 位有效数字写定。由于 \(\hat c\) 的误差远小于 \(10^{-17}\)，截断后的常量就是 \(c\) 的最佳双精度表示。这就是本目录反复出现 `workdps(50)` / `workdps(100)`、再用 `mpmath.nstr(x, 17, ...)` 截断的原因。

#### 4.2.2 核心流程：以 gammainc_asy 为例的系数推导链

`gammainc_asy.py` 计算 Temme 渐近展开的系数 \(d_{k,n}\)（DLMF 8.12.12），推导链由四个纯函数串成：

```text
compute_a(n)      # a_k，DLMF 5.11.6 的递推
   ↓
compute_g(K)      # g_k = sqrt(2)·(1/2)_k·a_{2k}，DLMF 5.11.3/5.11.5
   ↓
eta(λ)            # DLMF 8.12.1 的辅助函数，平移到 0 为中心
   ↓ mp.taylor(eta, 0, n-1)
compute_alpha(n)  # 对 eta 的 Taylor 系数做 Lagrange 反演 → alpha_n (DLMF 8.12.13)
   ↓
compute_d(K, N)   # 组合 alpha 与 g → d_{k,n} (DLMF 8.12.12)
```

其中 `compute_alpha` 用到的 Lagrange 反演正是 `utils.py` 提供的公共工具。

#### 4.2.3 源码精读

**公共工具 `lagrange_inversion`**——给定 \(f(x)\) 的级数 \(a_1 x+a_2 x^2+\cdots\)，求反函数级数 \(g(x)\) 使 \(f(g(x))=x\)。实现借助 sympy 的符号级数：先算 \(h=x/f\) 的幂级数，再按反演公式取系数。

[_precompute/utils.py:L12-L38](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/utils.py#L12-L38) —— 反演公式 \(b_k = \tfrac{1}{k}[x^{k-1}]\,h(x)^k\)，其中 \(h=x/f\)。注意它「naive 但易读」，因为这里**速度不是问题**（只离线算一次）。

**`gammainc_asy.compute_d`**——把 `alpha` 与 `g` 组合成最终的 \(d_{k,n}\) 表：

[_precompute/gammainc_asy.py:L55-L71](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/gammainc_asy.py#L55-L71) —— 递推 \(d_{k,n}=(-1)^k g_k d_{0,n} + (n+2)d_{k-1,n+2}\)，最后裁剪到 \(N\) 列。

**`main()` 的原子写**——把 \(25\times 25\) 的表写成 C 数组：

[_precompute/gammainc_asy.py:L94-L112](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/gammainc_asy.py#L94-L112) —— 在 `workdps(50)` 下算 `compute_d(25,25)`，每个数用 `mp.nstr(x, 17, min_fixed=0, max_fixed=0)` 截到 17 位科学计数，写入 `../cephes/igam.h.new` 后 `os.rename` 原子替换。

> ⚠️ **关于写入路径**：`main()` 把表写到 `../cephes/igam.h`（相对 `_precompute/` 即 `scipy/special/cephes/igam.h`）。这是 Cephes 库的遗留头文件路径——按 u3-l4 已建立的认知，Cephes 已收敛进新一代 `xsf` 库，该写入路径属于历史约定。本仓库的 `test_precompute_gammainc.py` 也**不**跑 `main()`，而是直接对 `compute_g/compute_alpha/compute_d` 这些纯函数做数值断言（见 4.4）。换言之，脚本里真正「在用、被测」的是数学推导函数，`main()` 的文件落盘是一次性手工动作。

**打印型脚本 `loggamma.py` / `zetac.py`**——不写文件，只 `print`，靠开发者肉眼拷贝：

[_precompute/loggamma.py:L9-L22](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/loggamma.py#L9-L22) —— Stirling 级数系数 \(\frac{B_{2n}}{2n(2n-1)}\) 与在 \(x=1\) 处的 Taylor 系数 \((-1)^n\zeta(n)/n\)，均在 `workdps(100)` 下算。

[_precompute/zetac.py:L8-L15](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/zetac.py#L8-L15) —— \(\zeta(x)-1\) 在 \(x=0\) 处的 Taylor 系数，用 `mpmath.diff(zeta, 0, n)/n!`。注意首项硬编码为 `-1.5`，因为 \(\zeta(0)=-0.5\)，故 \(\zeta(0)-1=-1.5\)。

**符号型脚本 `wright_bessel.py`**——与上述不同，它主要用 **sympy** 做符号推导，产出的是**可直接粘贴进 Cython 的代码片段字符串**（而非数值常量）：

[_precompute/wright_bessel.py:L159-L245](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/wright_bessel.py#L159-L245) —— `asymptotic_series()` 推导 Wright 函数大 \(x\) 渐近展开的系数 \(C_k\)（DLMF 10.46.E1），用正则把 sympy 表达式改写成 `A[k]`/`B[k]`/`Ap1[k]` 这种 C 数组下标形式，并特意给长整数加小数点以避开 32 位整型溢出。这体现了一个重要模式：**预计算脚本可以生成「代码」而不仅是「数据」**。

> **一个诚实的观察**：`expn_asy.py` 的 `main()` 看起来是半成品——若干行（如 `', '.join([...])`）是未被赋值的字符串表达式，写出的是字面 `{tmp}` 占位符（[L40-L50](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/expn_asy.py#L34-L50)）。这说明它并非当前实际在用的产物路径；真正被测的是纯函数 `generate_A`（见 4.4.2）。读者由此应建立一个判断：**衡量一个预计算脚本「是否在用」的依据，是测试是否覆盖它的纯函数，而不是 `main()` 是否能跑通。**

#### 4.2.4 代码实践：阅读 gammainc_asy 的头部注释与系数截断

1. **实践目标**：理解 `gammainc_asy.py` 预计算的是什么、为什么需要 50 位精度、系数如何截断到 17 位。
2. **操作步骤**：
   - 打开 [_precompute/gammainc_asy.py:L1-L10](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/gammainc_asy.py#L1-L10)，阅读文档字符串。注意它声明「about 8 hours to run」——这是一次性离线计算的典型耗时。
   - 阅读 [compute_d (L55-L71)](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/gammainc_asy.py#L55-L71)，确认 \(d_{k,n}\) 来自 DLMF 8.12.12 的递推。
   - 阅读 [main() 的截断与原子写 (L94-L112)](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/gammainc_asy.py#L94-L112)，定位 `mp.nstr(x, 17, ...)` 与 `os.rename`。
3. **需要观察的现象**：整张 \(d_{k,n}\) 表在一个 `with mp.workdps(50):` 块内算出，再被截断成 17 位科学计数法的 C 数组；写入用「先 `.new` 再 `rename`」两步。
4. **预期结果**：能用一句话回答「为什么是 50 位算、17 位写」——因为递推要高精度保数值，而落盘只需双精度最佳近似。

> 说明：本实践为**源码阅读型**，不建议本地真跑 `main()`（耗时极长且依赖遗留头文件路径存在）。如想验证计算正确性，应跑对应单元测试（见 4.4.2）。

#### 4.2.5 小练习与答案

**练习 1**：`zetac.py` 的 `zetac_series` 首项为什么硬编码成 `-1.5` 而不是用 `mpmath.zeta(0)` 算？

> **答案**：因为该级数是 \(\zeta(x)-1\) 在 \(x=0\) 处的 Taylor 展开，常数项为 \(\zeta(0)-1=(-\tfrac{1}{2})-1=-1.5\)。硬编码既避免依赖、又让读者一眼看出物理意义。

**练习 2**：`lagrange_inversion` 的文档字符串说自己「naive」，为什么这里不在乎效率？

> **答案**：它是离线预计算工具，一生只跑几次，跑完结果就固化进仓库；可读性远比速度重要，故选了最朴素的幂级数逐项相乘实现。

### 4.3 参考数据固化：从 mpmath 网格到 local.npz

#### 4.3.1 概念说明：为什么不能在测试里现算 mpmath

u9-l2 讲过，`test_mpmath.py` 用 mpmath 作实时参考。但对某些函数的某些参数区，mpmath 太慢或不收敛。`gammainc_data.py` 的文档字符串把这件事说得很直白：

> 「We can't just compare to mpmath's gammainc in test_mpmath.TestSystematic because it would take too long. ... To get around this we copy the mpmath implementation but use more terms.」

于是策略变成：**离线**用「魔改版 mpmath（允许更多项）」算好参考值，存成文本；测试只读取文本比对，不再碰 mpmath。这样既保住了「mpmath 黄金参考」的可信度，又把代价从「每次测试 17 分钟」摊成「开发期一次 17 分钟」。

#### 4.3.2 核心流程：参考数据的产消链路

```text
[开发期，手跑]
  gammainc_data.py ──► tests/data/local/gammainc.txt    (列: a, x, 期望值)
                        gammaincc.txt
  wright_bessel_data.py ──► tests/data/local/wright_bessel.txt (列: a, b, x, 期望值)

[构建期，自动]
  meson.build 的 custom_target 调 utils/makenpz.py
     把 tests/data/local/ 下所有 .txt 打包 ──► local.npz  (随包安装)

[测试期，自动]
  test_data.py: DATASETS_LOCAL = np.load(local.npz)
     data_local(gammainc, 'gammainc', (0,1), 2)  ──► FuncData(..., rtol=1e-12)
```

注意产消两端是**解耦**的：数据脚本只管写 `.txt`，对 `.npz` 一无所知；打包由构建系统的 `makenpz.py` 统一完成；测试只认 `.npz` 里的数组名。这样新增一个数据脚本只需「写 `.txt` + 在 `test_data.py` 加一行 `data_local(...)`」。

#### 4.3.3 源码精读

**数据生成 `gammainc_data.py`**——复制 mpmath 的 `hypercomb` 实现但放开 `maxterms`：

[_precompute/gammainc_data.py:L33-L52](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/gammainc_data.py#L33-L52) —— 注释直接指向 mpmath 源码行号 `mpmath/functions/expintegrals.py#L134`，`maxterms=10**8` 是关键改动；结果经 `mpf2float` 折回 double。

采样与落盘在 `main()` 里：在极坐标网格（\(r\) 从 \(10^4\) 到 \(10^{14}\)、角度按区分离散）上批量算，写文本：

[_precompute/gammainc_data.py:L89-L118](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/gammainc_data.py#L89-L118) —— 用 `np.logspace` 构造对数网格、`np.savetxt` 写 `(a, x, value)` 三列到 `tests/data/local/{func.__name__}.txt`。

**`mpf2float` 的舍入规避**——数据脚本和 u9-l2 都依赖它：

[_mptestutils.py:L308-L317](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_mptestutils.py#L308-L317) —— 先 `nstr(x, 17)` 把 `mpf` 转成 17 位十进制字符串，再 `float()`。直接 `float(mpf(...))` 会因二进制舍入丢失一位，这是数值库里典型的「打印再解析」反模式——用字符串做中转反而更准。

**构建期打包**——`meson.build` 把三个数据目录各打成一个 `.npz`：

[meson.build:L175-L208](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L175-L208) —— `npz_files` 列表里 `_data_local` 一项以 `tests/data/local/ellipkm1.txt` 为触发的 input、目录名 `local`、产出 `local.npz`；`custom_target` 调 `utils/makenpz.py` 把整个目录的 `.txt` 都塞进同一个 `.npz`，`install: true` 随包发布。

**测试消费**——`test_data.py` 加载并比对：

[tests/test_data.py:L54-L56](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_data.py#L54-L56) —— `data_local()` 把 `DATASETS_LOCAL[dataname]` 包成 `FuncData`（u9-l1）。

[tests/test_data.py:L660-L664](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_data.py#L660-L664) —— `data_local(gammainc, 'gammainc', (0, 1), 2, rtol=1e-12)` 表示：用 `local.npz['gammainc']` 的第 0、1 列当输入、第 2 列当期望，对 `scipy.special.gammainc` 做 `rtol=1e-12` 的比对。这正是 `_precompute/gammainc_data.py` 写出的那张表。

**变体：`hyp2f1_data.py` 的回归分析模式**——它不喂 `FuncData`，而是产出**可直接粘贴的测试用例源码**：

[_precompute/hyp2f1_data.py:L188-L228](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/hyp2f1_data.py#L188-L228) —— `make_hyp2f1_test_cases` 把 (a,b,c,z,rtol) 渲染成 `pytest.param(Hyp2f1TestCase(...), ...)` 字符串。整个脚本（默认 700MB 输出、约 40 分钟）是「离线大规模比对 → 挑选代表点 → 固化成参数化测试」的工程范式，文档字符串明确说它能用来「确保 hyp2f1 从 Fortran 迁到 Cython 时不回归」。

#### 4.3.4 代码实践：追踪一个参考数据点的产消全程

1. **实践目标**：把一个参考值从「mpmath 算出」到「测试比对」的完整链路走一遍。
2. **操作步骤**：
   - 在 [gammainc_data.py 的 main() (L97-L118)](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/gammainc_data.py#L97-L118) 确认：每个点存成 `(a, x, func(a,x))`，写进 `tests/data/local/gammainc.txt`。
   - 在 [meson.build (L188-L193)](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L188-L193) 确认 `tests/data/local/` 会被 `makenpz.py` 打包成 `local.npz` 并随包安装。
   - 在 [test_data.py:L660](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_data.py#L660) 确认 `data_local(gammainc, 'gammainc', (0,1), 2, rtol=1e-12)` 消费它。
3. **需要观察的现象**：数据脚本、构建规则、测试三处对「列布局 `(a,x,value)`」的约定必须**完全一致**——脚本写第 2 列为值、测试读第 2 列为期望，列号一旦错位就会全错。
4. **预期结果**：能解释为什么这条链路必须解耦成「`.txt`（人可读、可 diff）→ `.npz`（机器快读）→ `FuncData`」三段，而不是让测试直接调 mpmath。

> 如本地已装 SciPy，可观察消费端：`python -c "import importlib.resources as r, numpy as np; from scipy.special.tests import data; print(np.load(r.files(data)/'local.npz')['gammainc'].shape)"`（路径随版本可能不同；若取不到，标注「待本地验证」即可）。

#### 4.3.5 小练习与答案

**练习 1**：`gammainc_data.py` 为什么要「复制 mpmath 实现但用更多项」，而不是直接调 `mpmath.gammainc`？

> **答案**：mpmath 的 `gammainc` 内部用 `hypercomb`，且不允许用户调大最大项数，对大参数会抛 `NoConvergence`。复制实现并放开 `maxterms` 到 \(10^8\) 才能让这些点收敛，从而拿到可信参考值。

**练习 2**：`wright_bessel_data.py` 里为什么要把一批 `(a,b,x)` 显式列入 `failing` 数组并跳过？

> **答案**：这些点是已知在所需精度下 mpmath 也算不准或太慢的「困难角点」。与其让测试随机失败，不如显式剔除，并交由专门的 `test_wright_data_grid_failures` 之类测试单独跟踪（脚本注释明确指向该测试）。

### 4.4 构建与测试入口：脚本只被「安装」与「单测」，不被「运行」

#### 4.4.1 概念说明：_precompute/meson.build 只做安装

一个常见的误解是「既然 meson.build 里列了这些脚本，构建时就会跑它们」。**不是**。`_precompute/meson.build` 只有一条实质语句：

[_precompute/meson.build:L18-L21](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/meson.build#L18-L21) —— `py3.install_sources(python_sources, subdir: 'scipy/special/_precompute')` 把 13 个脚本原样**安装**进安装目录，使 `from scipy.special._precompute.xxx import ...` 在用户环境里可用（`test_precompute_*.py` 正是这么 import 的）。**没有任何 `custom_target`/`test()` 去执行它们。**

这是有意为之：预计算是一次性开发动作，产物已经提交进仓库（系数在头文件里、参考数据在 `tests/data/local/` 里），运行时和测试只读产物；若在每次构建时重跑，既要拖慢编译，又要强依赖 mpmath/sympy，还会因「8 小时」「17 分钟」这种耗时把 CI 拖垮。

#### 4.4.2 核心流程：测试如何覆盖预计算脚本

既然不跑 `main()`，怎么保证脚本没算错？答案在 `tests/test_precompute_*.py`——它们**直接 import 脚本里的纯计算函数**，用独立的、解析已知的参考值断言：

```text
test_precompute_gammainc.py
  test_g       → compute_g(7)  对比 DLMF 5.11.4 的解析值
  test_alpha   → compute_alpha(9) 对比 DLMF 8.12.14
  test_d       → compute_d(10,13) 对比 DiDonato & Morris (1986) 附录 F
  test_gammainc→ gammainc_data.gammainc 对比 mpmath.gammainc（实时）

test_precompute_expn_asy.py
  test_generate_A → generate_A(4) 对比 DLMF 8.20.5

test_precompute_utils.py
  TestInversion.test_log → lagrange_inversion(log 的系数) == exp 的系数
  TestInversion.test_sin → lagrange_inversion(sin 的系数) == asin 的系数
```

这是一种很漂亮的分层：**用更高层（解析公式/权威文献/反函数恒等式）的「已知正确答案」去校验底层推导函数**，而不是去校验 `main()` 写出的整个文件。

#### 4.4.3 源码精读

**对系数表逐元素断言**——`test_d` 拿 DiDonato & Morris 论文附录里的数值当真值：

[tests/test_precompute_gammainc.py:L51-L84](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_precompute_gammainc.py#L51-L84) —— `compute_d(10,13)` 后取若干 \((k,n)\) 点，与论文里抄写的 `mp.mpf('...e-...')` 值用 `mp_assert_allclose`（u9-l2）比对，`workdps(50)`。

**对参考数据生成器做实时 mpmath 回检**——`test_gammainc` 用 `assert_mpmath_equal`（u9-l2）：

[tests/test_precompute_gammainc.py:L88-L95](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_precompute_gammainc.py#L88-L95) —— 在小参数区（`Arg(0,100)`）实时比对 `gammainc_data.gammainc` 与 `mpmath.gammainc`，`rtol=1e-17`、`dps=50`。这验证「魔改版 mpmath」在小参数下与原版一致，从而间接保证大参数下落盘数据的可信度。

**用恒等式校验反演**——`lagrange_inversion` 的正确性靠「反函数的级数应等于已知反函数的级数」：

[tests/test_precompute_utils.py:L21-L36](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_precompute_utils.py#L21-L36) —— `lagrange_inversion(log(1+x) 的系数)` 应等于 `exp(x)-1 的系数`；`sin 的系数` 应等于 `asin 的系数`。这是纯数学恒等式作 oracle。

**依赖与并发护栏**——三个测试文件共享同一套装饰器约定：

[tests/test_precompute_gammainc.py:L1-L23](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_precompute_gammainc.py#L1-L23) —— `MissingModule` 让缺 mpmath/sympy 时测试被跳过而非报错；`@check_version(mp,'0.19')` 守版本；`pytest.mark.thread_unsafe` 标注「mpmath 的 gmpy2 后端非线程安全」；`@pytest.mark.slow`/`xslow` 把重计算挪到慢测试档。

#### 4.4.4 代码实践：在 meson.build 与测试中定位入口

1. **实践目标**：澄清「脚本的构建入口」与「测试入口」分别在哪、各做什么。
2. **操作步骤**：
   - 打开 [_precompute/meson.build:L1-L22](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_precompute/meson.build#L1-L22)，确认其中**只有** `py3.install_sources`，没有任何 `custom_target` 或 `test(`。
   - 用检索工具列出 `tests/test_precompute_*.py` 三个文件，确认它们 `from scipy.special._precompute.xxx import compute_*`。
   - 对照 [test_precompute_gammainc.py 的 test_d (L51-L84)](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_precompute_gammainc.py#L51-L84)，确认它测的是 `compute_d`（纯函数），而不是 `main()`。
3. **需要观察的现象**：`_precompute/meson.build` 与 `test_precompute_*.py` 都**不**触发 `main()` 的文件落盘。
4. **预期结果**：能纠正「meson.build 列出的脚本会被构建期执行」的误解，并解释这样设计（一次性离线、产物固化、CI 不重跑）的原因。

#### 4.4.5 小练习与答案

**练习 1**：假如你想新增一个预计算脚本 `foo_data.py` 往 `tests/data/local/foo.txt` 写参考数据，需要改哪几处？

> **答案**：① 写脚本本身；② 在 `_precompute/meson.build` 的 `python_sources` 里加上它（以便安装后能被 import/重跑）；③ 在 `tests/test_data.py` 加一行 `data_local(foo, 'foo', ..., rtol=...)`。`meson.build` 顶层的 `local.npz` 规则会自动把新 `.txt` 打包，无需改。

**练习 2**：为什么 `test_precompute_gammainc.py` 既测 `compute_d`（对比论文），又测 `gammainc`（对比 mpmath）？

> **答案**：前者校验「系数推导函数」对解析已知答案正确（覆盖模式 A）；后者校验「参考数据生成器」在小参数下与原版 mpmath 一致，从而为其在大参数下落盘的数据可信度背书（覆盖模式 B）。两者分别守住两条产物链。

## 5. 综合实践

**任务：为 `_precompute/` 画一张「输入→产物→消费者」的全景图，并诊断一个脚本的「健康度」。**

1. **测绘产消链**：任选两个脚本——一个系数型（`gammainc_asy.py`）、一个数据型（`gammainc_data.py`）。沿下面三条线分别追踪到底：
   - `gammainc_asy.py`：`compute_*` 纯函数 →（被）`test_precompute_gammainc.py` 校验；`main()` → `../cephes/igam.h`（遗留路径）。
   - `gammainc_data.py`：`gammainc/gammaincc` → `tests/data/local/gammainc.txt` → `meson.build` 的 `makenpz` → `local.npz` → `test_data.py` 的 `data_local(...)` → `FuncData`。
2. **诊断健康度**：对一个预计算脚本，用以下三问判断它「是否真的在用」：
   - 它有没有对应的 `tests/test_precompute_*.py`？（若否，可能已半废弃，如 `expn_asy.main()` 的 `{tmp}` 占位符所暗示。）
   - 它的产物（系数/数据）能否在仓库里找到消费者？
   - 它的 `main()` 是否还能跑通（路径依赖、依赖库是否齐全）？
3. **产出**：一张表格，列出 13 个脚本各自的「类型（系数/数据/回归）」「产物去向」「测试覆盖情况」。这张表本身就是一份维护文档——它回答了「如果我要改某函数的数值实现，哪些参考数据/系数需要重算」。

> 这个综合实践把本讲三块内容（动机与模式、系数生成、数据固化与测试入口）串成一条可操作的维护流程，也正是 SciPy 维护者在改动特殊函数内核时实际要做的事。

## 6. 本讲小结

- `_precompute/` 是**开发工具集**而非运行时依赖：把「推导繁、计算慢」的系数与参考数据离线算好、固化进仓库，运行时和测试只读产物。
- 脚本分三类：**系数生成型**（写 C 头文件常量或打印供手工拷贝）、**参考数据型**（写 `tests/data/local/*.txt`）、**回归分析型**（如 `hyp2f1_data.py` 直接生成 `pytest.param` 源码）。
- 高精度策略统一为「`workdps(50~1000)` 算、`nstr(...,17)` 截断」，并用 `mpf2float` 规避 `float(mpf)` 的舍入陷阱；`lagrange_inversion` 等公共工具放在 `utils.py`。
- 参考数据链路解耦三段：脚本写 `.txt`（人可读）→ `meson.build` 调 `makenpz.py` 打包成 `local.npz`（随包安装）→ `test_data.py` 的 `data_local()` 喂给 `FuncData`。
- `_precompute/meson.build` **只安装、不运行**这些脚本；真正的正确性保障来自 `tests/test_precompute_*.py`，它们用解析公式（DLMF）、权威论文（DiDonato & Morris）和数学恒等式（反函数级数）去校验脚本里的**纯计算函数**，而非 `main()`。
- 判断一个脚本是否「在用」的可靠依据是**测试是否覆盖它的纯函数**，而不是 `main()` 是否能跑通——`expn_asy.main()` 的 `{tmp}` 占位符就是反例。

## 7. 下一步学习建议

- **回到 u9-l1/u9-l2**：把本讲与它们连起来看——u9-l1 的 `FuncData` 是数据脚本产物的「消费者」，u9-l2 的 `Arg/mpf2float/assert_mpmath_equal` 是数据脚本的「零件供应商」。三者合起来就是 special 的「数值正确性保障体系」。
- **顺着一个产物下钻到内核**：选 `gammainc`，从 `_precompute/gammainc_asy.py` 的系数表出发，找到运行时非完整 Gamma 内核里消费这张表的代码（Cephes/xsf 路径，见 u3-l4、u8-l1），体会「离线系数 → 编译进内核 → 运行时查表」的全链路。
- **动手加一条参考数据**：仿照 `wright_bessel_data.py` 的网格采样与 `failing` 过滤模式，试着为一个你感兴趣的 special 函数写一个最小数据脚本，跑通「写 `.txt` → 加 `data_local(...)` → 测试通过」的闭环，作为对整条产消链的实战验证。
- **延伸阅读**：DLMF（Digital Library of Mathematical Functions）是本目录反复引用的权威来源，建议挑一节（如 §8.12 Temme 渐近展开）对照 `gammainc_asy.py` 读，理解「文献公式 → 代码递推」的翻译过程。
