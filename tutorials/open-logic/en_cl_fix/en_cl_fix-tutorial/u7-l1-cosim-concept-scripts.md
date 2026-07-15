# 协同仿真概念与 cosim 脚本

## 1. 本讲目标

本讲进入 en_cl_fix 的「验证闭环」核心环节——**协同仿真（co-simulation，简称 cosim）**。读者学完后应该能够：

- 理解 en_cl_fix 采用的「Python 参考模型生成黄金数据 + VHDL 测试台逐拍比对」的验证思想，以及为什么这种思想能保证 VHDL 与 Python 行为完全一致。
- 读懂任意一个 `bittrue/cosim/<操作>/cosim.py` 脚本：它如何遍历格式参数、如何用 `cl_fix_*_fmt` 构造合法结果格式、如何用参考模型算出期望输出。
- 读懂公共工具 `cosim_utils.py`：`get_data` 如何穷举一个格式的所有可能取值，`ProgressReporter` 如何上报进度，`clear_directory` 如何清空数据目录。
- 亲手运行一个 cosim 脚本，看到它落地了哪些数据文件，并理解每个文件在验证链路里扮演的角色。

本讲只讲「黄金数据是怎么生成的」，**不讲** VHDL 测试台如何读取这些数据（留待 u7-l2），也不讲 `sim/run.py` 如何把 cosim 挂到 VUnit 上（留待 u7-l3）。

## 2. 前置知识

在进入本讲前，读者应已具备以下认知（来自前置讲义）：

- **三语言镜像架构（u1-l2）**：VHDL 是语义金标准，Python 是同名同参数的参考模型，二者构成「镜像」。MATLAB 只是薄封装。验证的目标就是证明 VHDL 实现与 Python 参考模型在数值上逐位一致。
- **Python 主接口（u4-l1）**：`en_cl_fix.py` 中的 `cl_fix_*` 函数与 VHDL 包同义；算术函数遵循「预测全精度中间格式 `mid_fmt` → 无损运算 → `cl_fix_resize` 收敛」三段式。本讲会直接调用这些函数作为「黄金模型」。
- **舍入模式 FixRound（u2-l2）与结果格式预测（u3）**：`FixFormat.for_round` 能在综合期预测「按某舍入模式舍入后的最小合法结果格式」。本讲的 cosim 脚本正是用 `for_round` 来派生合法输出格式，而不是随意枚举 `[S,I,F]`。
- **VHDL 包头类型（u5-l1）**：知道 `FixFormat_t` / `FixRound_t` 是什么，理解 cosim 生成的数据最终要喂给 VHDL UUT（被测单元）比对。

两个本讲要用到、但值得再强调的小概念：

- **未归一化整数（unnormalized integer）**：定点格式 `[S,I,F]` 的真实数值 \(v\) 与其「原始整数位串」\(d\) 的关系是 \(v = d \cdot 2^{-F}\)。也就是说把存储的整数乘以 \(2^{-F}\) 才得到真实数值。反过来 `cl_fix_to_integer` 做的是 \(d = v \cdot 2^{F}\)。这个 \(d\) 就是硬件 `std_logic_vector` 被当作有/无符号整数解释后的值——黄金数据正是以这个 \(d\) 形式保存的。
- **穷举（exhaustive）验证**：对每个被测格式，遍历它**所有**可能的取值，而不是随机采样。这样能覆盖每一个边界位翻转，是位级正确性的最强保证。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [bittrue/cosim/cl_fix_round/cosim.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cl_fix_round/cosim.py) | `cl_fix_round` 操作的 cosim 脚本，本讲的主剖析对象。它遍历输入/输出格式与舍入模式，生成黄金数据文件。 |
| [bittrue/cosim/cosim_utils.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cosim_utils.py) | 所有 cosim 脚本共用的工具：`clear_directory`、`get_data`、`repeat_*`、`ProgressReporter`。 |
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py) | Python 主接口，cosim 调用它作为「黄金模型」：`cl_fix_round`、`cl_fix_to_integer`、`cl_fix_from_integer`、`cl_fix_write_formats` 等。 |

补充参照（非本讲重点，用于把概念讲清楚）：

| 文件 | 作用 |
| --- | --- |
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py) | `FixRound` 枚举定义与 `FixFormat.for_round` 实现。 |
| [sim/cosim_runner.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/cosim_runner.py) | 把 cosim 脚本的 `run()` 作为 VUnit `pre_config` 回调线程安全地执行一次（u7-l3 详讲）。 |

仓库里一共有 13 个操作目录，每个目录结构完全一致（一个 `cosim.py`，运行后多出一个 `data/`）：

```
bittrue/cosim/
├── cosim_utils.py          ← 公共工具（本讲重点之二）
├── cl_fix_round/
│   ├── cosim.py            ← 本讲主剖析对象
│   └── data/               ← 运行后生成，存黄金数据
├── cl_fix_add/
│   ├── cosim.py
│   └── data/
└── ... (cl_fix_sub, cl_fix_mult, cl_fix_resize, cl_fix_saturate, ...)
```

理解了 `cl_fix_round/cosim.py`，就能举一反三读懂其余 12 个。

## 4. 核心概念与源码讲解

### 4.1 协同仿真的核心思想：Python 黄金模型 + HDL 比对

#### 4.1.1 概念说明

en_cl_fix 的验证目标非常苛刻：**VHDL 实现必须在每一个位、每一种格式组合下都与 Python 参考模型完全一致**。直接写 VHDL 测试台、在 VHDL 里手算期望值是不现实的——定点舍入/饱和在边界情况下的正确结果很难手算，而且手算本身就可能出错。

于是项目采用了一种经典的「黄金参考（golden reference）」思路，把「算期望值」这件事完全交给已经过单独测试的 Python 参考模型：

> 用 Python 把「输入」和「期望输出」都预先算好、存成文件；VHDL 测试台只负责把同样的输入喂给 UUT，然后把 UUT 的输出与文件里的期望输出逐拍比对。

这就把问题从「VHDL 算得对不对」转化成了「VHDL 输出是否等于 Python 输出」——一个可机械判定的问题。因为 Python 模型与 VHDL 共享同一套 `[S,I,F]` 语义与舍入/饱和定义（镜像架构），二者一致就等价于实现正确。

#### 4.1.2 核心流程

整个验证闭环分为三段，本讲只深入第一段（生成）：

```
┌─────────────────────────┐     ┌──────────────────────┐     ┌─────────────────────┐
│ 1. Python cosim 脚本     │     │ 2. data/ 黄金文件     │     │ 3. VHDL 测试台       │
│    (本讲重点)            │ ──> │    a_fmt.txt          │ ──> │    读文件、驱动 UUT、 │
│  遍历格式 + 模式          │     │    r_fmt.txt          │     │    逐拍比对          │
│  用参考模型算期望输出      │     │    rnd.txt            │     │    (u7-l2)           │
│                          │     │    testN_output.txt   │     │                     │
└─────────────────────────┘     └──────────────────────┘     └─────────────────────┘
        ↑ cl_fix_* 作为黄金模型              ↑ 中间产物                    ↑ 被验证对象
```

第一段的关键设计决策有四点：

1. **穷举输入值**：对每个被测输入格式，遍历它所有可能的取值（见 4.3 的 `get_data`），而不是随机采样。这是位级正确性的最强保证。
2. **格式参数也遍历**：不仅遍历数据值，还遍历「输入格式、输出格式、舍入/饱和模式」这些参数本身，从而覆盖大量格式组合。
3. **输出格式由 `cl_fix_*_fmt` 派生**：不随意枚举输出 `[S,I,F]`，而是用结果格式预测函数算出「合法且最小」的输出格式（见 4.2）。
4. **期望输出以整数形式落盘**：保存的是未归一化整数 \(d = v \cdot 2^{F}\)，即硬件位串的整数值，方便 VHDL 直接比对（见 4.4）。

#### 4.1.3 源码精读

`cosim.py` 的入口是一个普通的 `run()` 函数，可被 `cosim_runner` 作为回调调用，也可直接当脚本执行：

[bittrue/cosim/cl_fix_round/cosim.py:L38-L41](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cl_fix_round/cosim.py#L38-L41) —— `run()` 入口，第一步先清空 `data/` 目录，确保每次都是干净重生成。

文件末尾的标准 `__main__` 守卫让脚本可独立运行：

[bittrue/cosim/cl_fix_round/cosim.py:L130-L131](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cl_fix_round/cosim.py#L130-L131) —— 支持 `python cosim.py` 直接执行，这正是本讲代码实践的入口。

而 `cosim_runner` 在仿真侧把这个 `run()` 包成 VUnit 的 `pre_config` 回调，保证它在仿真启动前、线程安全地、**至多执行一次**：

[sim/cosim_runner.py:L60-L72](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/cosim_runner.py#L60-L72) —— `cosim_runner.run()` 拿着一把本地锁 + 自禁用标志，确保同一个 cosim 即使被多个 VUnit 配置回调也不会重复跑（u7-l3 详讲）。

#### 4.1.4 代码实践

1. **实践目标**：建立对「cosim 脚本就是黄金数据生成器」的直觉。
2. **操作步骤**：用编辑器打开 `bittrue/cosim/cl_fix_round/cosim.py`，只看顶层 `run()` 的骨架（先忽略循环细节），确认它做了「清目录 → 遍历参数 → 算输出 → 存文件」四件事。再打开任意另一个操作目录（如 `bittrue/cosim/cl_fix_neg/cosim.py`），对比它们的结构是否一致。
3. **需要观察的现象**：两个脚本的「清目录、建列表、双层循环、结尾 `cl_fix_write_formats`」骨架几乎逐行相同，差异只在「有几个输入、调哪个 `cl_fix_*`、遍历哪些模式」。
4. **预期结果**：你会确信「读懂一个 cosim.py = 读懂全部 13 个」，这就是本讲以 `cl_fix_round` 为单一剖析对象的依据。

#### 4.1.5 小练习与答案

**练习 1**：为什么 en_cl_fix 不直接在 VHDL 测试台里计算期望输出，而要绕一圈用 Python 生成黄金文件？

> **参考答案**：因为定点运算（尤其舍入、饱和的边界情况）的正确结果手算极易出错，而 Python 参考模型已经过独立的单元测试（见 u8-l3）、与 VHDL 共享同一套语义定义。把「算期望值」交给 Python，就把验证问题从「VHDL 算得对不对」降维成了「VHDL 输出是否等于 Python 输出」，后者可机械、可穷举地判定。

**练习 2**：cosim 脚本既能被 `cosim_runner` 当回调调用，又能被 `python cosim.py` 直接运行。这两种入口分别服务于什么场景？

> **参考答案**：`python cosim.py` 直接运行用于开发者单独调试黄金数据生成（本讲代码实践即用此入口）；`cosim_runner` 回调用于正式仿真流程，由 `sim/run.py` 在 VUnit 启动仿真前自动触发，并保证同名脚本线程安全地只跑一次。

---

### 4.2 run() 的参数遍历与 cl_fix_*_fmt 构造

#### 4.2.1 概念说明

`cl_fix_round` 的行为由三个自由度决定：输入格式 `a_fmt`、目标小数位数 `rF`、舍入模式 `rnd`。cosim 的核心工作就是**把这三个自由度各自取一批「测试点」，做笛卡尔积穷举**，对每一个组合生成一组黄金数据。

这里有一个精妙之处：输出格式 `r_fmt` 不是自由枚举的，而是用 `FixFormat.for_round(a_fmt, rF, rnd)` **派生**出来的。原因有二：

- **合法性**：`cl_fix_round` 内部有断言 `assert r_fmt == cl_fix_round_fmt(a_fmt, r_fmt.F, rnd)`，要求调用者传入的 `r_fmt` 必须恰好等于预测格式。随意枚举 `r_fmt` 会大量触发断言失败。
- **最小性**：`for_round` 给出的是能精确表示舍入结果的最小格式（见 u3-l3），用它当输出格式既合法又不过宽，正好检验「舍入真正发生」的场景。

#### 4.2.2 核心流程

`run()` 的遍历逻辑是四层嵌套循环，伪代码如下：

```
清空 data/
test_count = 0
for aS in {0,1}:                  # 输入符号位
  for aI in [-5 .. 5]:            # 输入整数位
    for aF in [-5 .. 5]:          # 输入小数位
      若 aS+aI+aF < 1: 跳过        # 位宽必须 ≥ 1
      a_fmt = FixFormat(aS, aI, aF)
      a = get_data(a_fmt)          # 穷举该格式所有取值（见 4.3）
      for rF in [-5 .. 5]:         # 目标小数位
        for rnd in 全部 7 种 FixRound:
          r_fmt = FixFormat.for_round(a_fmt, rF, rnd)   # 派生合法输出格式
          r = cl_fix_round(a, a_fmt, r_fmt, rnd)         # 黄金模型算期望输出
          把 r 存成 test{test_count}_output.txt（见 4.4）
          记录 a_fmt / r_fmt / rnd 到列表
          test_count += 1
把所有 a_fmt / r_fmt / rnd 列表存成文件（见 4.4）
```

注意遍历范围是有意控制的小区间（`[-5,5]`）。因为 `get_data` 会穷举每个格式的**所有**取值，格式一大，组合数会爆炸。把 `I`、`F` 限制在 `[-5,5]`、`S` 限制在 `{0,1}`，是在「覆盖足够多格式形态」与「运行时间可控」之间取的折中。

#### 4.2.3 源码精读

参数「测试点」集中定义在 Config 段：

[bittrue/cosim/cl_fix_round/cosim.py:L47-L53](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cl_fix_round/cosim.py#L47-L53) —— `aS_values=[0,1]`、`aI_values=aF_values=np.arange(-5,1+5)`、`rF_values=np.arange(-5,1+5)`。注意 `np.arange(-5, 1+5)` 产生 `[-5,-4,...,5]` 共 11 个值。

四层嵌套循环的主体，含「跳过不可用格式」与「派生输出格式」两个关键点：

[bittrue/cosim/cl_fix_round/cosim.py:L69-L96](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cl_fix_round/cosim.py#L69-L96) —— 第 76–77 行 `if aS+aI+aF < 1: continue` 跳过位宽不足 1 的非法格式（与 `FixFormat` 的 `I+F>=0` 约束呼应，见 u2-l1）；第 93 行 `r_fmt = FixFormat.for_round(a_fmt, rF, rnd)` 派生合法输出格式；第 96 行 `r = cl_fix_round(a, a_fmt, r_fmt, rnd)` 调用黄金模型算期望输出。

`for_round` 的实现回顾（来自 u3-l3）：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py:L319-L342](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L319-L342) —— 非 `Trunc_s` 模式且 `rF < a_fmt.F` 时整数位 `+1`（舍入可能进位），并保证结果至少 1 位宽。cosim 依赖这个函数给出合法 `r_fmt`。

`cl_fix_round` 主接口内部那条「格式契约」断言：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:L190-L212](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L190-L212) —— 第 194 行 `assert r_fmt == cl_fix_round_fmt(a_fmt, r_fmt.F, rnd)` 强制调用者必须传对 `r_fmt`，这正是 cosim 用 `for_round` 派生而非随意枚举的根本原因。

顺带一提，cosim 还在循环内「夹带」了一个 WideFix 一致性自检（见 4.3.3），它不是黄金数据的一部分。

#### 4.2.4 代码实践

1. **实践目标**：直观感受「格式参数穷举 + `for_round` 派生」的组合规模。
2. **操作步骤**：在仓库根目录运行下面这段一次性 Python（不修改任何源码，只是内省）：

   ```python
   # 示例代码：估算 cl_fix_round cosim 的测试用例总数
   import numpy as np
   aS_values = [0,1]; aI_values = np.arange(-5,6); aF_values = np.arange(-5,6)
   rF_values = np.arange(-5,6)
   from collections import Counter
   from bittrue.models.python.en_cl_fix_pkg.en_cl_fix_types import FixFormat, FixRound
   cnt = 0
   for aS in aS_values:
       for aI in aI_values:
           for aF in aF_values:
               if aS+aI+aF < 1: continue
               a_fmt = FixFormat(aS, aI, aF)
               for rF in rF_values:
                   for rnd in FixRound:
                       _ = FixFormat.for_round(a_fmt, rF, rnd)
                       cnt += 1
   print("test cases:", cnt)
   ```

   （路径导入按你的运行方式调整；最简单是在 `bittrue/cosim/cl_fix_round/` 目录下仿照 `cosim.py` 顶部那两段 `sys.path.append` 来设置。）
3. **需要观察的现象**：打印出一个几千量级的整数，对应 `run()` 结尾那句 `Cosim generated {test_count} tests.` 的输出。
4. **预期结果**：你会理解为什么格式测试点要限在 `[-5,5]`——再放宽一倍，用例数和数据量都会成倍膨胀。
5. 若环境不便导入包，可改为「源码阅读型实践」：手工数 `aS(2) × aI(11) × aF(11)` 种输入格式候选，扣除位宽不足者，再乘以 `rF(11) × rnd(7)`，估算数量级即可——**待本地验证**精确值。

#### 4.2.5 小练习与答案

**练习 1**：如果把第 93 行改成 `r_fmt = FixFormat(0, 8, rF)`（随意固定一个输出格式）会怎样？

> **参考答案**：大部分组合下，`cl_fix_round` 内部第 194 行的断言 `r_fmt == cl_fix_round_fmt(a_fmt, r_fmt.F, rnd)` 会失败并抛 `AssertionError`，脚本直接中断。这正说明输出格式必须由 `for_round` 派生，不能随意指定。

**练习 2**：为什么遍历的是 `rF`（目标小数位）而不是完整的输出 `[S,I,F]`？

> **参考答案**：因为对 `cl_fix_round` 而言，输出格式由「输入格式 + 目标小数位 + 舍入模式」完全确定（符号位 S 沿用输入、整数位 I 由 `for_round` 推导、小数位就是 rF）。枚举 `rF` 已经覆盖了所有有意义的输出格式，再枚举 S/I 只会产生非法或冗余组合。

---

### 4.3 黄金输入：get_data 穷举所有取值

#### 4.3.1 概念说明

「穷举验证」要求对每个被测格式，把它的**每一个可能取值**都送进 UUT。一个 `[S,I,F]` 格式总共有 \(W = S+I+F\) 位，因此共有 \(2^{W}\) 个不同取值（有符号时是补码范围）。

`get_data(fmt)` 就是干这件事的：它先算出该格式的整数取值范围 \([d_{\min}, d_{\max}]\)，用 `np.arange` 生成这个范围内的**每一个整数**，再用 `cl_fix_from_integer` 还原成定点数据。这样得到的数组就包含了该格式全部 \(2^{W}\) 个取值，无一遗漏。

#### 4.3.2 核心流程

```
get_data(fmt):
  d_min = cl_fix_to_integer(cl_fix_min_value(fmt), fmt)   # 最小取值的整数
  d_max = cl_fix_to_integer(cl_fix_max_value(fmt), fmt)   # 最大取值的整数
  int_data = np.arange(d_min, 1+d_max)                    # [d_min .. d_max] 闭区间
  return cl_fix_from_integer(int_data, fmt)               # 整数 → 定点数据
```

数学上，格式 \([S,I,F]\) 的整数范围为：

\[
d_{\min} = \begin{cases} -2^{S+I+F-1} & S=1 \\ 0 & S=0 \end{cases}, \qquad
d_{\max} = \begin{cases} 2^{S+I+F-1}-1 & S=1 \\ 2^{S+I+F}-1 & S=0 \end{cases}
\]

对应真实数值范围 \([d_{\min}\cdot 2^{-F},\ d_{\max}\cdot 2^{-F}]\)（见 u2-l4 的 `cl_fix_min_value`/`max_value`）。`np.arange(d_min, 1+d_max)` 用 `1+d_max` 是因为 `arange` 是半开区间，要包含 `d_max` 必须加 1。

#### 4.3.3 源码精读

`get_data` 只有 4 行，是穷举思想的最纯粹体现：

[bittrue/cosim/cosim_utils.py:L40-L45](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cosim_utils.py#L40-L45) —— 先用 `cl_fix_to_integer` 把格式的最小/最大值转成整数边界，`np.arange` 生成闭区间内全部整数，`cl_fix_from_integer` 还原为定点数组。

它依赖的两个主接口函数：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:L145-L156](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L145-L156) —— `cl_fix_from_integer`：把未归一化整数 \(d\) 包装成格式 `fmt` 的定点数据（\(v=d\cdot 2^{-F}\)）。例：`cl_fix_from_integer(5, FixFormat(0,2,1))` 得到 2.5。

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:L159-L170](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L159-L170) —— `cl_fix_to_integer`：反向操作 \(d=v\cdot 2^{F}\)。注意无论 F 正负，结果都是整数：F 为正时是放大，F 为负时因可表示值本就是 \(2^{|F|}\) 的倍数，除回去仍是整数。wide 格式则直接返回原始整数（第 167–168 行）。

`cl_fix_round` 的 cosim 在拿到 `a = get_data(a_fmt)` 后，紧接着做了一次 WideFix 自检（注意：这**不是**黄金数据的一部分，而是借循环顺带验证 wide 路径与 narrow 路径结果一致）：

[bittrue/cosim/cl_fix_round/cosim.py:L82-L101](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cl_fix_round/cosim.py#L82-L101) —— 第 82 行穷举输入；第 83、100–101 行把同一份输入转成 `WideFix` 再 round，断言其 `to_real()` 与 narrow 路径的 `cl_fix_round` 结果逐元素相等。注释明确说明这是「not actually part of the cosim data generation」。

> 对二元运算（如 `cl_fix_add`），`get_data` 只穷举单个格式还不够，还需要把 a、b 两组全取值做完全配对。`cosim_utils` 提供了 `repeat_whole_array` / `repeat_each_value` 两个铺平助手来完成笛卡尔积（本讲的 `cl_fix_round` 是一元运算，用不到它们）：
> [bittrue/cosim/cosim_utils.py:L47-L51](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cosim_utils.py#L47-L51) —— `repeat_whole_array(a, n)` 把 a 整体重复 n 次，`repeat_each_value(b, n)` 把 b 每个元素重复 n 次，二者等长配对即得 a×b 全组合。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到 `get_data` 对一个小格式返回了「全部」取值。
2. **操作步骤**：在 `bittrue/cosim/cl_fix_round/` 目录下，仿照 `cosim.py` 顶部设置好 `sys.path`，然后运行：

   ```python
   # 示例代码
   from cosim_utils import get_data
   from en_cl_fix_pkg import cl_fix_to_real, FixFormat
   fmt = FixFormat(1, 2, 0)        # 3 位有符号：可表示 -4,-3,-2,-1,0,1,2,3
   a = get_data(fmt)
   print("count:", a.size)                         # 期望 8
   print("values:", cl_fix_to_real(a, fmt))        # 期望 -4 .. 3 全部 8 个值
   ```

3. **需要观察的现象**：`a.size` 恰为 \(2^{W}=2^{3}=8\)，且 `cl_fix_to_real` 打印出从 -4 到 3 的连续整数序列，无重复无遗漏。
4. **预期结果**：确认 `get_data` 是真正的穷举——这正是「位级正确性」保证的来源。把 `fmt` 改成 `FixFormat(0,2,1)`（4 位无符号、F=1）应得到 16 个值，从 0 到 1.875 步长 0.5。
5. 若运行环境无 Python 包，可改为阅读 `cl_fix_add/cosim.py` 第 96–112 行，理解二元运算如何用 `repeat_*` 把两个穷举数组配对——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`get_data` 里为什么写 `np.arange(d_min, 1+d_max)` 而不是 `np.arange(d_min, d_max)`？

> **参考答案**：`np.arange` 是半开区间 `[start, stop)`，不含 `stop`。要包含最大值 `d_max`，`stop` 必须取 `d_max+1`。少写那个 `1+` 会漏掉格式的最大取值，破坏穷举性。

**练习 2**：`get_data(FixFormat(0,8,0))` 会返回多少个元素？为什么 cosim 脚本不直接测这么大的格式？

> **参考答案**：会返回 \(2^{8}=256\) 个元素。但 cosim 是「格式参数 × 数据值」的双重穷举：格式一大，单组数据值就多，再乘以大量格式组合和 7 种舍入模式，总数据量和运行时间会爆炸。所以脚本把 `I`、`F` 测试点限在 `[-5,5]` 的小区间，用「小格式但全覆盖」换取可运行性。

---

### 4.4 黄金输出落盘：cl_fix_to_integer + np.savetxt + cl_fix_write_formats

#### 4.4.1 概念说明

算出期望输出 `r` 之后，要把它存成 VHDL 测试台能读取的文件。这里有一个关键的表示问题：**存浮点还是存整数？**

项目选择存**未归一化整数** \(d = v\cdot 2^{F_r}\)（\(F_r\) 是结果格式的小数位）。原因是：这个 \(d\) 就是硬件 `std_logic_vector` 被当作有/无符号整数解释后的值。VHDL 测试台只需把 UUT 输出的位串转成整数、再和文件里的整数比对即可，无需在 VHDL 里做任何浮点或定点换算——既简单又精确。

每个测试用例落盘成两类文件：

- **数据文件** `testN_output.txt`：该用例的期望输出整数序列（一列整数，带一个说明长度的 header）。
- **参数文件**：`a_fmt.txt`（每个用例的输入格式）、`r_fmt.txt`（每个用例的输出格式）、`rnd.txt`（每个用例的舍入模式序号）。测试台靠这些文件知道「第 N 组数据该用什么格式、什么模式去驱动 UUT 和解读输出」。

#### 4.4.2 核心流程

数据落盘与参数收集在循环内同步进行：

```
for ... 每个测试用例:
    r = cl_fix_round(...)                              # 黄金输出（定点）
    np.savetxt(f"test{N}_output.txt",
               cl_fix_to_integer(r, r_fmt),            # 转成整数位串值
               fmt="%i", header=f"r[{r.size}]")
    test_a_fmt.append(a_fmt)                           # 暂存参数
    test_r_fmt.append(r_fmt)
    test_rnd.append(rnd.value)
    N += 1
# 循环结束后，把三组参数列表整体落盘
cl_fix_write_formats(test_a_fmt, ["a_fmt0",...], "a_fmt.txt")
cl_fix_write_formats(test_r_fmt, ["r_fmt0",...], "r_fmt.txt")
np.savetxt("rnd.txt", test_rnd, fmt="%i", header="Rounding modes")
```

`cl_fix_to_integer(r, r_fmt)` 在这里扮演「定点 → 整数位串」的翻译器：把以 \(v\) 形式存在的黄金输出，转成以 \(d\) 形式存在的整数，`np.savetxt(..., fmt="%i")` 再把整数写成纯文本。

#### 4.4.3 源码精读

循环内保存期望输出（注意 header 里写的是元素个数 `r.size`，供测试台预分配）：

[bittrue/cosim/cl_fix_round/cosim.py:L104-L106](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cl_fix_round/cosim.py#L104-L106) —— `np.savetxt` 把 `cl_fix_to_integer(r, r_fmt)` 的整数数组以 `%i` 格式写入 `test{test_count}_output.txt`，header 标注元素个数。

参数先暂存到列表、循环结束后再统一落盘：

[bittrue/cosim/cl_fix_round/cosim.py:L108-L125](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cl_fix_round/cosim.py#L108-L125) —— 第 109–111 行把每个用例的 `a_fmt`、`r_fmt`、`rnd.value` 追加进列表；第 118–122 行用 `cl_fix_write_formats` 把两个格式列表写成 `a_fmt.txt` / `r_fmt.txt`；第 125 行用 `np.savetxt` 把舍入模式序号列表写成 `rnd.txt`。

`cl_fix_write_formats` 把一组 `FixFormat` 对象序列化为文本（每行一个 `"(S,I,F)"` 字符串，首行是名字列表注释）：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:L458-L472](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L458-L472) —— 写一行 `# name1,name2,...` 头注释，随后每个格式调 `cl_fix_format_to_string`（即 `str(fmt)`）写一行。这个文件正是 VHDL 端 `cl_fix_format_from_string` 解析的输入（见 u5-l4 的字符串解析）。

`cl_fix_to_integer` 把黄金输出转成整数位串值（与 4.3.3 同一函数，此处强调它在落盘中的角色）：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:L159-L170](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L159-L170) —— narrow 路径返回 `NarrowFix(a, a_fmt).to_integer()`（即 \(v\cdot 2^{F}\)），wide 路径直接返回原始整数。无论哪条路径，结果都是「硬件位串的整数值」，可与 UUT 输出直接比对。

#### 4.4.4 代码实践（本讲主实践任务）

1. **实践目标**：亲手运行 `cl_fix_round` 的 cosim 脚本，观察它生成了哪些黄金数据文件，并理解每个文件的作用。
2. **操作步骤**：
   ```bash
   cd bittrue/cosim/cl_fix_round
   python cosim.py
   ls -1 data/
   ```
   再用 `head` 查看几个文件的前几行，例如：
   ```bash
   head -n 3 data/a_fmt.txt
   head -n 3 data/r_fmt.txt
   head -n 1 data/rnd.txt
   head -n 3 data/test0_output.txt
   ```
3. **需要观察的现象**：`data/` 下出现 `a_fmt.txt`、`r_fmt.txt`、`rnd.txt`，以及若干 `testN_output.txt`（N 从 0 到 `test_count-1`）；控制台末尾打印 `Cosim generated <N> tests.`。
4. **预期结果与文件作用对照表**：

   | 文件 | 内容 | 作用 |
   | --- | --- | --- |
   | `a_fmt.txt` | 每行一个 `"(S,I,F)"`，共 N 行（首行名字注释） | 告诉测试台第 i 组用例的**输入格式** |
   | `r_fmt.txt` | 同上 | 告诉测试台第 i 组用例的**输出格式** |
   | `rnd.txt` | 一列整数（0–6），共 N 行 | 告诉测试台第 i 组用例的**舍入模式**（`FixRound` 的 `value`） |
   | `testN_output.txt` | 一列整数（header 标注元素个数） | 第 N 组用例的**期望输出**（整数位串值），供逐拍比对 |

5. 若当前环境无法运行 Python（缺 numpy 等），改为「源码阅读型实践」：对照本节「文件作用对照表」，在 `cosim.py` 第 104–125 行找到每个文件分别由哪条语句生成，并说明 `rnd.txt` 存的是 `rnd.value`（枚举序号 0–6）而非字符串——**待本地验证**运行结果。

#### 4.4.5 小练习与答案

**练习 1**：为什么期望输出存的是 `cl_fix_to_integer(r, r_fmt)`（整数）而不是 `r`（浮点真实值）？

> **参考答案**：因为整数 \(d=v\cdot 2^{F_r}\) 恰好等于 UUT 输出位串被当作有/无符号整数解释的值。VHDL 测试台只需把 UUT 的 `std_logic_vector` 转成整数直接比对，不必在 VHDL 里做浮点/定点换算，既精确又高效。存浮点反而会引入「VHDL 端如何把位串转浮点」的额外验证负担。

**练习 2**：`rnd.txt` 里存的是 `rnd.value`。对照 `FixRound` 枚举，序号 `1` 代表哪种舍入模式？

> **参考答案**：`FixRound` 枚举按 `Trunc_s=0, NonSymPos_s=1, NonSymNeg_s=2, SymInf_s=3, SymZero_s=4, ConvEven_s=5, ConvOdd_s=6` 定义（见 [en_cl_fix_types.py:L30-L40](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L30-L40)）。序号 `1` 即 `NonSymPos_s`（半向上、非对称正舍入）。

---

### 4.5 cosim_utils 配套：ProgressReporter 与 clear_directory

#### 4.5.1 概念说明

穷举生成几千组数据需要一点时间，`ProgressReporter` 负责在终端按百分比打印进度（每完成 10% 打一次），让长时间运行有可见反馈，避免误以为卡死。`clear_directory` 则在每次 `run()` 开头把旧的 `data/` 删掉重建，保证黄金数据始终与当前脚本一致、不留陈旧文件。

这两个工具与定点语义无关，纯属「脚手架」，但它们是所有 13 个 cosim 脚本共享的公共零件，理解它们有助于快速读懂任何一个 cosim。

#### 4.5.2 核心流程

`ProgressReporter` 的工作方式：

```
ProgressReporter((list1, list2, list3)):    # 传入若干参数取值列表
    total = len(list1) * len(list2) * len(list3)   # 笛卡尔积总数
    next_percent = 0
    index = 0
report():           # 每完成一个最外层迭代调一次
    index += 1
    percent = 100 * index / total
    若 percent >= next_percent:
        打印 "next_percent%..."
        next_percent += 10
    若 index == total: 打印 "Done."
```

`clear_directory(path)`：先尝试 `rmtree(path)` 删除整棵旧目录（不存在则忽略），再 `os.mkdir(path)` 重建空目录。

#### 4.5.3 源码精读

`clear_directory` 容错地清空并重建目录：

[bittrue/cosim/cosim_utils.py:L33-L38](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cosim_utils.py#L33-L38) —— `try/except FileNotFoundError` 保证首次运行（目录还不存在）时不报错，删完立刻 `os.mkdir` 重建。

`ProgressReporter` 用笛卡尔积总数计算百分比：

[bittrue/cosim/cosim_utils.py:L58-L83](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cosim_utils.py#L58-L83) —— `__init__` 第 60–61 行用 `np.prod` 算各参数列表长度的乘积作为总数；`report` 第 67–83 行按 `index/total` 推进百分比，每跨过一个 10% 门槛打印一次，全部完成打印 `Done.`。

`cl_fix_round` 的 cosim 在最外层循环用它上报进度（注意它只统计 `a_fmt` 三元组的外层进度，内层 `rF`/`rnd` 不计入）：

[bittrue/cosim/cl_fix_round/cosim.py:L68-L73](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cl_fix_round/cosim.py#L68-L73) —— `ProgressReporter((aS_values, aI_values, aF_values))` 构造，每个 `a_fmt` 候选调一次 `progress.report()`。

#### 4.5.4 代码实践

1. **实践目标**：观察 cosim 运行时的进度打印与目录重建行为。
2. **操作步骤**：先在 `bittrue/cosim/cl_fix_round/data/` 下手动放一个无关文件（如 `touch data/stale.txt`，若 `data/` 不存在就先 `mkdir data`）；然后运行 `python cosim.py`，观察终端输出与目录变化。
3. **需要观察的现象**：终端先打印 `Generating cosim data: 0%...10%...20%...` 一直到 `Done.`，随后打印 `Cosim generated <N> tests.`；运行结束后 `data/stale.txt` 消失，只剩新生成的黄金文件。
4. **预期结果**：印证 `clear_directory` 的「先删后建」语义，以及 `ProgressReporter` 的 10% 步进上报。
5. 若无法运行，改为阅读 `ProgressReporter.report` 第 67–83 行，说明为何首次调用会立即打印 `0%...`（因为 `index==0` 分支先打印起始消息，随后 percent 从 0 起步）——**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`ProgressReporter` 只把 `(aS_values, aI_values, aF_values)` 计入总数，没算 `rF` 和 `rnd`。这样得到的百分比反映的是什么？

> **参考答案**：反映「外层 `a_fmt` 三元组的遍历进度」。因为进度只在最外层每个 `a_fmt` 候选处上报一次（第 73 行），内层 `rF`、`rnd` 的迭代不被单独计入。所以百分比衡量的是「输入格式空间的完成度」，而非「测试用例总数」的完成度——后者是前者的 \(11\times 7\) 倍。

**练习 2**：为什么 `clear_directory` 用 `try/except FileNotFoundError` 包住 `rmtree`？

> **参考答案**：首次运行时 `data/` 目录可能尚不存在，`rmtree` 会抛 `FileNotFoundError`。捕获并忽略它，让脚本能干净地「无则建、有则先删后建」，无论目录是否预先存在都能正常工作。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一次「迷你 cosim」的阅读 + 复现：

**任务**：为 `cl_fix_neg`（取反，一元运算）画一张「黄金数据生成流程图」，并回答下面一组连环问题。

1. **读结构**：打开 [bittrue/cosim/cl_fix_neg/cosim.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cl_fix_neg/cosim.py)，确认它的骨架与 `cl_fix_round/cosim.py` 是否一致（清目录、Config 段、四层循环、结尾 `cl_fix_write_formats`）。
2. **认自由度**：`cl_fix_neg` 的行为由哪些自由度决定？它的 cosim 遍历了哪些参数、调用了哪个 `cl_fix_*_fmt` 来派生中间/输出格式？（提示：见 u3-l2 的 `for_neg`、u4-l1 的 `cl_fix_neg` 三段式。）
3. **追数据**：黄金输入怎么来的（`get_data`）？期望输出由哪个黄金模型函数算出（`cl_fix_neg`）？以什么形式落盘（`cl_fix_to_integer` → `np.savetxt`）？
4. **比差异**：与 `cl_fix_round` 相比，`cl_fix_neg` 的输出文件多了还是少了？（提示：`cl_fix_neg` 带 `rnd` 与 `sat` 两个模式参数，参考 `cl_fix_add/cosim.py` 同时写 `rnd.txt` 和 `sat.txt`。）
5. **动手跑**：在 `bittrue/cosim/cl_fix_neg/` 下运行 `python cosim.py`，列出 `data/` 下生成的文件，对照本讲 4.4.4 的「文件作用对照表」逐个解释。

完成这个任务后，你应该能不依赖任何讲义、独立读懂仓库里**任意一个** cosim 脚本——这就是本讲的终极目标。

> 说明：第 5 步若当前环境无 Python/numpy，可只完成 1–4 步的源码阅读部分，并明确标注「运行结果待本地验证」。

## 6. 本讲小结

- en_cl_fix 的验证采用「Python 参考模型生成黄金数据 + VHDL 测试台逐拍比对」的协同仿真思想，把「VHDL 算得对不对」降维成「VHDL 输出是否等于 Python 输出」。
- 每个 `cosim.py` 的 `run()` 做四件事：清 `data/` → 遍历格式参数与模式 → 用 `cl_fix_*` 黄金模型算期望输出 → 把输出与参数落盘。
- 输出格式不由人随意枚举，而是用 `FixFormat.for_round`（即 `cl_fix_round_fmt`）派生——这既满足 `cl_fix_round` 内部的格式契约断言，又保证输出格式合法且最小。
- 黄金输入用 `get_data` **穷举**每个格式的全部取值（整数闭区间 `np.arange(d_min, 1+d_max)`），这是位级正确性的最强保证；格式测试点限在 `[-5,5]` 以控制组合爆炸。
- 期望输出以**未归一化整数** \(d=v\cdot 2^{F}\) 形式存盘（`cl_fix_to_integer` + `np.savetxt`），因为它等于 UUT 位串的整数值，便于 VHDL 直接比对；格式与模式分别存入 `a_fmt.txt`/`r_fmt.txt`/`rnd.txt`。
- `cosim_utils` 提供 `clear_directory`（先删后建）、`get_data`（穷举）、`repeat_*`（二元配对）、`ProgressReporter`（10% 步进上报）四个公共脚手架，被全部 13 个 cosim 脚本复用。

## 7. 下一步学习建议

本讲只完成了验证闭环的**第一段**（黄金数据生成）。建议按以下顺序继续：

- **u7-l2（VUnit 测试台与文件 I/O）**：看 VHDL 测试台 `tb/cl_fix_round_tb.vhd` 如何反向读取本讲生成的 `a_fmt.txt` / `r_fmt.txt` / `rnd.txt` / `testN_output.txt`，如何驱动 UUT、如何逐拍比对并报错。这是闭环的第二段。
- **u7-l3（run.py 装配）**：看 `sim/run.py` 如何用 `cosim_runner` 把本讲的 `run()` 挂成 VUnit `pre_config` 回调、如何为每个 TB 绑定 cosim 与 `meta_width` 配置。这是闭环的第三段。
- **u8-l3（Python 单元测试）**：本讲把 Python 当作「黄金模型」无条件信任。u8-l3 会展示这个黄金模型本身是如何被 `bittrue/tests/python/` 下的测试（对照 numpy 参考实现、`format_tests` 的充分必要性断言）验证的——这是信任的源头。
- **延伸阅读**：对比 [bittrue/cosim/cl_fix_add/cosim.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cl_fix_add/cosim.py) 与本讲的 `cl_fix_round/cosim.py`，体会一元运算与二元运算（需 `repeat_whole_array`/`repeat_each_value` 配对）在 cosim 结构上的差异。
