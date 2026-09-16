# 零拷贝模式：视图别名与安全约束

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 MoonEP 中两次「边界拷贝」（dispatch 输出侧、combine 输入侧）的确切位置、字节代价，以及 `zero_copy=True` 分别省掉了哪一次。
2. 掌握 `zero_copy` 与 `router_weights_zero_copy` 两个开关的**分层语义**：它们独立门控 hidden 与 route weights 两条张量通路，且后者默认 `False`。
3. 理解「视图别名」的物理本质：dispatch 返回的不是普通张量，而是 NVL 对称内存接收区的一个切片，任何一个 rank 的下一次通信都会覆盖它。
4. 能写出与 `combine` 内部完全等价的 `data_ptr()` 精确别名断言，并判断一个框架集成场景（推理 / 训练 + autograd）何时必须退回 `zero_copy=False`。

## 2. 前置知识

本讲默认你已读过 u1-l4（Buffer API）、u2-l2（对称内存）与 u6-l1（异步通信流）。再用三段通俗语言补齐本讲专属的前置概念。

**（a）PyTorch 的视图与 `data_ptr()`。** 一个张量对象由「元信息（形状、dtype、步长）+ 指向一块存储（storage）的指针」组成。切片、`view()`、`reshape()` 等操作只新建元信息、不复制数据，得到的就是**视图（view）**；两个视图若指向同一个存储起始地址，就说它们**别名（alias）**同一块内存。`t.data_ptr()` 返回张量首元素的原始字节地址——它与 dtype 无关，所以 `int32` 张量 `view(torch.float32)` 之后 `data_ptr()` 不变，这一点正是 MoonEP 跨 dtype 别名检查的基础。相反，`clone()` / `empty_like()` 会分配新存储，`data_ptr()` 必然不同，**即使数值完全相等**。

**（b）边界拷贝是什么。** MoonEP 的通信内核（dispatch、combine）永远以 NVL 对称缓冲 `hidden_buf_local` 为读写中枢：dispatch 把 token 从远端写进它，combine 从它读出。但用户手里的张量通常是另一块普通显存，于是默认路径上出现了两次纯本地拷贝——dispatch 结束时把 `hidden_buf_local` **拷出**到新分配的返回张量，combine 开始时把用户张量**拷入** `hidden_buf_local`。这两次拷贝不参与跨 rank 通信，纯粹是为了让返回值/入参拥有独立生命周期，故称「边界拷贝」（代码注释里的 master-style boundary copies）。

**（c）autograd 为什么会「保存」张量。** PyTorch 反向传播需要前向的某些中间结果（例如分块 GEMM 的输入用于算权重梯度）。autograd 引擎会在构建计算图时把这些张量的引用存进各算子的 `ctx`（`save_for_backward`），到 `backward()` 执行时才读取。如果一个张量在「被保存」与「被读取」之间被别人原地改写，梯度就会**静默算错**——不报错、不越界，只是数值不对。这是本讲风险模型的核心。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `moonep/api.py` | 本讲主角：两个视图属性、两条边界拷贝路径、分层开关与别名断言全部在此 |
| `moonep/buffer.py` | 视图背后的地基：`create_nvl_dist_tensor` 分配的 VMM 对称内存（u2-l2 已精读，本讲只取一角） |
| `tests/test_e2e.py` | 零拷贝往返的官方测试范本（`zero_copy round-trip` 段落），综合实践直接以它为模板 |
| `README.md` | 官方 `zero_copy` 用法与安全警告（§ zero_copy） |
| `benchmarks/bench_vs_deepep.py` | 性能路径实证：对比基准全程以 `zero_copy=True` 运行 |

## 4. 核心概念与源码讲解

### 4.1 零拷贝路径：边界拷贝的位置与代价

#### 4.1.1 概念说明

回顾 u4-l2/u4-l6：dispatch 内核经 TMA 把每个 token 直写到**目的 rank** 的 `hidden_buf` 接收区，combine 内核从本 rank 的接收区读出并 K 求和。也就是说，无论用户怎么设置开关，**NVL 缓冲永远是内核侧的唯一数据中枢**；`zero_copy` 控制的不是内核行为，而是「内核与用户张量之间要不要多一次拷贝」。

`zero_copy=False`（默认）时，一次前向有两次边界拷贝：

- **dispatch 输出侧拷出**：`hidden_buf_local` → 新分配的 `[NvS, H]` 返回张量；
- **combine 输入侧拷入**：用户的 `[NvS, H]` 张量 → `hidden_buf_local`。

`zero_copy=True` 时这两次拷贝都被跳过：dispatch 直接把 `hidden_buf_local` 本身返回给用户，专家 FFN 在这块内存上原地读写，combine 直接消费它。省下的显存带宽流量为

\[
\Delta = \underbrace{2}_{\text{拷出+拷入}} \times \underbrace{2}_{\text{读+写}} \times \mathrm{NvS} \times H \times 2\,\text{B} = 8\,\mathrm{NvS}H\ \text{字节}
\]

以 \(S=4096, K=8, H=7168\)、\(\mathrm{NvS} \approx S \cdot K = 32768\) 估算，每层前向每 rank 约 \(8 \times 32768 \times 7168 \approx 1.9\) GB 额外 DRAM 流量——在 3 TB/s 量级的 HBM 上约零点几毫秒，层数多时相当可观（此为纸面估算，具体收益**待本地验证**）。反向链路（combine bwd 的 dispatch、dispatch bwd 的 combine）还有对称的两次，训练全步共省约两倍。官方基准也据此口径把边界拷贝排除在计时外：`bench_comm.py` 明确注明 `zero_copy=False` 的边界 `tensor.copy_` 是普通 torch 算子、不属于 MoonEP 内核，`zero_copy=True` 时直接消失（[benchmarks/bench_comm.py:L58-L59](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L58-L59)）。

#### 4.1.2 核心流程

两种模式下 dispatch / combine 的数据通路（`HB` 代表 `hidden_buf_local`）：

```text
zero_copy = False（默认）：
  dispatch:  远端 token ─TMA→ HB ─copy_→ 新张量(返回)
  combine:   用户张量 ─copy_→ HB ─combine内核→ 新张量 [S,H](返回)

zero_copy = True：
  dispatch:  远端 token ─TMA→ HB(本身即返回值，无拷贝)
  combine:   HB(要求用户已原地写好) ─combine内核→ 新张量 [S,H](返回)
```

要点：

1. 两次拷贝在两条 `_run_*_on_current_stream` 私有方法里，紧跟在通信内核之后/之前，同流发射（异步路径下即通信流，见 u6-l1）。
2. combine 的**输出** `hidden_sh` 永远是新分配张量——零拷贝只作用于输入侧；输出是 token-major 普通内存，与 NVL 缓冲无关。
3. 拷贝是纯 torch `copy_` 算子，故路由权重通路可以借 `view` 做 fp32↔int32 的位重解释（见 4.2.3）。

#### 4.1.3 源码精读

先看 dispatch 侧。`_run_dispatch_on_current_stream` 末尾，边界拷出被两个开关分别门控：

- [moonep/api.py:L653-L661](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L653-L661)：dispatch 内核与 epilogue 发射完毕后，`if not zero_copy` 才把 `hidden_buf_local` 拷出到返回张量 `hidden_nvsh`；`if not route_weights_zero_copy` 才把 `weights_buf_local`（int32 位模式）拷出到 `route_weights_nvs`。开关为 True 时这两段 `copy_` 完全不执行——返回值就是 NVL 缓冲视图本身（视图在 dispatch 入口处已选定，见 4.2.3）。

再看 combine 侧，方向相反——拷入：

- [moonep/api.py:L680-L687](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L680-L687)：`if not zero_copy` 时先把用户张量 `hidden_nvsh` 拷入 `hidden_buf_local`，`if not router_weights_zero_copy` 时把 fp32 入参经 `.view(torch.int32)` 拷入 `weights_buf_local`，然后才进入 combine prologue / combine 内核。注意权重拷入用了 `.view(torch.int32)`——meta 缓冲是 int32 的，路由权重以 fp32 位模式存放在其中（u2-l4 的 WEIGHTS 区）。

最后确认输出侧恒为新张量：

- [moonep/api.py:L1022-L1036](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1022-L1036)：combine 为 `hidden_sh`（`[S,H]`）与 `route_weights_sk`（`[S,K]`）各自 `torch.empty` 新分配——combine 不存在「输出零拷贝」概念。

#### 4.1.4 代码实践

**实践目标**：把 4.1.1 的字节公式变成一个可核对的计算脚本（纯 CPU 可运行，无需 GPU）。

**操作步骤**：

1. 新建 `boundary_bytes.py`，读入一组 `(S, K, E, R, H, token_padding, n_layers)` 配置。
2. 用 u2-l1 的公式 `NvS = S*K + (token_padding-1)*2*E/R` 计算逻辑槽位数（取 `NvS_padded ≈ NvS` 作下界近似即可，注明近似）。
3. 按公式 \(\Delta_{fwd} = 8\,\mathrm{NvS}H\) 字节、训练全步 \(\Delta_{step} \approx 2\Delta_{fwd}\)，打印每层/每步的省流量，并除以一个假设带宽（如 3 TB/s）折算时间。
4. 用两组配置对比：`(S=4096, K=8, E=256, R=8, H=7168, tp=128)` 与 `(S=8192, K=4, E=64, R=8, H=4096, tp=128)`。

**需要观察的现象**：`K` 翻倍时 `NvS` 与省流量近似线性增长；层数（如 60 层 MoE）放大后的每 step 总省流量是否达到数十 GB 量级。

**预期结果**：第一组配置 \(\Delta_{fwd} \approx 1.9\) GB/层，60 层训练步约 220 GB（纸面估算）。真实收益受 HBM 有效带宽、拷贝实现影响，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `zero_copy=True` 用在 dispatch 但 combine 仍用 `zero_copy=False`，边界拷贝省掉了几次？剩下一次发生在哪个方向？

**答案**：省掉一次（dispatch 输出侧拷出），剩下 combine 输入侧的拷入——用户的 FFN 通常无法保证恰好写在 NVL 缓冲上时，combine 仍需把普通张量搬进接收区。两个开关在 combine 上是独立参数，允许这种混合用法（dispatch 侧由返回值是谁决定，combine 侧由自己的 `zero_copy` 参数决定）。

**练习 2**：为什么 combine 的输出 `hidden_sh` 不提供「零拷贝返回视图」选项？

**答案**：`hidden_sh` 是 `[S,H]` token-major 结果，由 combine 内核从 NVL 缓冲 K 求和后写出；它不在 `hidden_buf`/`meta_buf` 里，没有可复用的常驻通信缓冲。而且输出张量的生命周期就是用户的，没有「会被下次通信覆盖」的问题，也就没有省拷贝的机会。

### 4.2 分层开关：`zero_copy` 与 `router_weights_zero_copy`

#### 4.2.1 概念说明

dispatch/combine 各传递**两条**张量通路：payload（hidden，`[NvS,H]` bf16，大头）与路由权重（route weights，`[NvS]` fp32，很小）。MoonEP 用两个参数分别门控它们，而不是一个总开关：

- `zero_copy`：只管 hidden 通路；
- `router_weights_zero_copy`：只管路由权重通路，**即使 `zero_copy=True` 也默认 `False`**。

分层的动机写在 dispatch 的 docstring 里：路由权重张量小（省的拷贝只有 \(4\mathrm{NvS}\) 字节，收益可忽略），却是「危险特例」——训练框架极容易把它存进 autograd 状态（加权求和的权重在 backward 时还要用）。所以设计者把它单独拆出来，要求推理这类「下次 dispatch 前一定消费完」的调用方**显式**选择加入。这个拆分来自 commit `b5a0e7f`（"Split hidden/route-weights zero-copy control via router_weights_zero_copy"），此前一个 `zero_copy` 同时控制两者。

#### 4.2.2 核心流程

把 dispatch 输出侧与 combine 输入侧组合成开关矩阵：

| 通路 | dispatch 输出侧开关 | 开关=False 时 dispatch | combine 输入侧开关 | 开关=False 时 combine |
| --- | --- | --- | --- | --- |
| hidden `[NvS,H]` | `zero_copy` | `empty_like` 新张量 + 拷出 | `zero_copy` | 拷入 `hidden_buf_local` |
| 权重 `[NvS]` fp32 | `router_weights_zero_copy` | `empty` 新 fp32 张量 + 拷出 | `router_weights_zero_copy` | 拷入 `weights_buf_local` |

行为组合（前向 `dispatch → FFN → combine`）：

```text
(False, False)：两次拷贝全在，生命周期最安全（默认）
(True,  False)：hidden 零拷贝；权重走普通张量——框架只保存权重时够用
(True,  True) ：全零拷贝，仅限「下次通信前消费完两条视图」（如推理）
```

#### 4.2.3 源码精读

**dispatch 的返回值选择**——开关在这里就决定了返回的是视图还是新张量：

- [moonep/api.py:L797-L808](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L797-L808)：`zero_copy=True` 时 `hidden_nvsh` 直接取 `ctx['hidden_buf_local']`（视图），否则 `torch.empty_like` 新分配；`router_weights_zero_copy=True` 时返回 `ctx['weights_buf_local'].view(torch.float32)`（int32 位缓冲重解释成 fp32 视图），否则新分配 fp32 张量。注意 `.view(torch.float32)` 不改 `data_ptr()`——这为 combine 侧的跨 dtype 断言铺路。

**官方文档语义**（dispatch docstring 的两段）：

- [moonep/api.py:L753-L760](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L753-L760)：`zero_copy` 的完整契约——返回 `hidden_buf_local` 视图、视图会被本 Buffer 的下一次 dispatch/combine 覆盖、autograd 不得保存（这正是必须退回 `False` 的场景）、行内容仅在 `cu_seqlens` 覆盖的 padded 段内有定义。
- [moonep/api.py:L761-L767](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L761-L767)：`router_weights_zero_copy` 为何独立且默认 False——权重视图是被训练框架存入 autograd 状态的最常见形态，只有「下次 dispatch 前消费完」（如推理）的调用方才应显式开启。

**combine 的输入侧契约与断言**：

- [moonep/api.py:L977-L985](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L977-L985)：combine docstring——`zero_copy=True` 要求 `hidden_nvsh` **恰好是**某次 `zero_copy=True` dispatch 返回的视图，调用方 FFN 已原地写好输出，不做边界拷贝，用 `data_ptr()` 断言。
- [moonep/api.py:L1009-L1020](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1009-L1020)：两条断言本体。hidden 侧比较 `hidden_nvsh.data_ptr() == ctx['hidden_buf_local'].data_ptr()`；权重侧比较 `route_weights_nvs.data_ptr() == ctx['weights_buf_local'].data_ptr()`。**数值相同不算数，必须是同一地址**——`clone()` 出来的副本会在这里被拦下。

阅读小提示：dispatch 公开参数名是 `router_weights_zero_copy`（[moonep/api.py:L729-L731](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L729-L731)），但它传给 `_run_dispatch_on_current_stream` 时改名为 `route_weights_zero_copy`（[moonep/api.py:L819-L822](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L819-L822)），combine 侧则全程 `router_weights_zero_copy`——读源码时别被两个近义名迷惑，指同一开关。

#### 4.2.4 代码实践

**实践目标**：不运行代码，纯靠阅读补全 2×2 开关矩阵下「dispatch 返回什么 / combine 做什么」的真值表，并用测试验证其中一格。

**操作步骤**：

1. 画出 4 行真值表：行 = `(zero_copy, router_weights_zero_copy)` 四种组合，列 = `hidden_nvsh 是否视图`、`route_weights_nvs 是否视图`、`combine 侧发生几次拷贝`。
2. 打开 [tests/test_e2e.py:L346-L361](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L346-L361)，找到 `(True, True)` 这一格的实测代码：dispatch 以 `zero_copy=True, router_weights_zero_copy=True` 调用，随后 `combine` 以同样的两个开关消费视图。
3. 核对你的真值表与 docstring [moonep/api.py:L753-L767](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L753-L767) 逐句一致。

**需要观察的现象**：`(True, False)` 组合下 dispatch 返回「hidden 是视图、权重是新张量」的混合体——这正是训练框架只保存权重时的折中形态。

**预期结果**：真值表 4 行全部能从 L797-L808 与 L680-L687 两段源码直接推出，无需记忆。运行验证**待本地验证**（需多卡）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `router_weights_zero_copy` 的收益（约 \(4\mathrm{NvS}\) 字节/次）与 hidden 通路（\(2\mathrm{NvS}H\) 字节/次）相差三个数量级，MoonEP 还要为它单独设一个开关，而不是让它跟随 `zero_copy`？

**答案**：因为零拷贝的本质是「用生命周期约束换带宽」。hidden 视图又大又常被框架立刻消费，风险/收益比划算；权重视图虽小，却几乎必然被训练框架存进 autograd 状态（backward 加权求和要用），风险高而收益低。拆成独立开关并默认 False，等价于强迫高风险用户显式表态——一个 API 设计上的「安全默认值」案例。

**练习 2**：`combine(router_weights_zero_copy=True)` 传入的 fp32 张量与 `weights_buf_local`（int32）dtype 不同，`data_ptr()` 断言为何能成立？

**答案**：dispatch 返回的权重视图本身就是 int32 缓冲经 `.view(torch.float32)` 得到的（L804）；`view(dtype)` 只重解释位模式、不改存储与起始地址，所以该 fp32 视图的 `data_ptr()` 与 int32 切片完全相等，断言比较的是原始字节地址，与 dtype 无关。

**练习 3**：某推理框架在 combine 之后仍需保留 `route_weights_nvs` 做日志，可以开 `router_weights_zero_copy=True` 吗？

**答案**：可以，但必须先 `clone()` 再保留——在 combine 返回后、下一次 dispatch/combine 前完成克隆是安全的；直接保留视图则不行，下次通信（哪怕是别的 rank 发起的 dispatch）会覆盖它。

### 4.3 缓冲区视图属性：把接收区交给用户

#### 4.3.1 概念说明

零拷贝闭环需要两半：dispatch 返回视图（输出侧），以及用户能把 FFN 输出**写进**接收区（输入侧）。后者不只靠 dispatch 的返回值——`Buffer` 还把两个视图暴露为只读属性，供「zero-copy 集成」使用：调用方（例如自定义 MoE 模块）可以预先拿到 `hidden_nvsh_buffer_view`，把自己的分组 GEMM 输出直接写到这块内存上，再把同一视图递给 `combine(zero_copy=True)`。这覆盖了 dispatch 返回值覆盖不到的场景——例如 combine bwd 时梯度重派发后、或框架希望自行控制写入节奏时。

两个属性与 dispatch 返回的视图**是同一个对象级别的同一块内存**（属性每次访问重新切片，但地址相同），因此别名断言对二者都成立。

#### 4.3.2 核心流程

视图的构造链（自底向上）：

```text
buffer.py: create_nvl_dist_tensor([NvS_padded, H], bf16)   ← VMM 对称内存，第 r 段物理在 rank r
api.py L361:        hidden_buf                                ← [R*NvS_padded, H] 全组映射
api.py L432:        hidden_buf_local = hidden_buf[rank段][:NvS]   ← 本 rank 接收区，逻辑 NvS 行
api.py L514-524:    hidden_nvsh_buffer_view → hidden_buf_local

buffer.py/meta:     meta_buf（int32，含 WEIGHTS 区，见 u2-l4）
api.py L433-434:    weights_buf_local = meta_buf[rank段 + WEIGHTS_OFF][:NvS]
api.py L526-532:    router_weight_buffer_view → weights_buf_local.view(fp32)
```

对 GPU 而言，`hidden_buf_local` 就是本卡普通 HBM 上的一块 `[NvS, H]` bf16——专家 GEMM 读写它没有任何性能惩罚；它「特殊」仅在于远端 rank 也能通过 NVLink 写它（dispatch 的 S2G），以及它常驻不释放、被反复覆写。

#### 4.3.3 源码精读

**两个视图属性及其安全契约**：

- [moonep/api.py:L514-L524](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L514-L524)：`hidden_nvsh_buffer_view` 返回本 rank 的 `[NvS, H]` bf16 通信缓冲。docstring 给出两条铁律：供 zero-copy 集成使用（把专家输出写进此视图再交回 `combine(zero_copy=True)`）；视图别名的是**每次 dispatch/combine 都会覆盖的常驻通信状态**——绝不能让它（或任何与其共享 storage 的张量）进入 autograd 保存状态。
- [moonep/api.py:L526-L532](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L526-L532)：`router_weight_buffer_view` 返回 `weights_buf_local` 的 fp32 视图，docstring 明言「与 `hidden_nvsh_buffer_view` 相同的别名/生命周期规则」。

**视图在 context 中的来源**（构造期一次切好）：

- [moonep/api.py:L431-L434](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L431-L434)：`hidden_buf_local` 是 `hidden_buf` 第 `rank` 段的前 `NvS` 行（物理段长 `NvS_padded`，尾部 VMM 对齐行不暴露）；`weights_buf_local` 是 `meta_buf` 第 `rank` 段 WEIGHTS 区的 `NvS` 个 int32。
- [moonep/api.py:L361-L364](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L361-L364)：两块底层缓冲的分配入口。`hidden_buf` 经 [moonep/buffer.py:L217-L241](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L217-L241) 的 `create_nvl_dist_tensor` 建立（u2-l2 的 VMM 分配-映射两步模型）；`meta_buf` 走 `create_nvl_dist_multicast_tensor`（u2-l4）。

**测试中的属性用法**（别名关系的官方验证方式）：

- [tests/test_e2e.py:L349-L354](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L349-L354)：测试直接断言 `h_zc.data_ptr() == buffer._require_ctx()['hidden_buf_local'].data_ptr()`——dispatch 的零拷贝返回值就是接收区视图；随后还断言视图内容与 `zero_copy=False` 的拷出张量逐位相等（`torch.equal`），证明零拷贝不改变数值语义。

#### 4.3.4 代码实践

**实践目标**：在纯 CPU 环境验证 4.3.1 声称的视图/别名/位重解释语义（无需 GPU 与 MoonEP）。

**操作步骤**：运行以下脚本（示例代码，非项目源码）：

```python
import torch

# 1) 切片视图共享地址
buf = torch.zeros(1024, 8, dtype=torch.bfloat16)   # 假想的 hidden_buf
local = buf[512:512 + 128]                          # 假想的 hidden_buf_local
assert local.data_ptr() != buf.data_ptr()           # 偏移后地址不同
assert local[0].data_ptr() == local.data_ptr()      # 子视图同址

# 2) clone 数值相等但地址不同 —— combine 断言会拒绝它
copy = local.clone()
assert torch.equal(copy, local)
assert copy.data_ptr() != local.data_ptr()          # "数值相等 ≠ 别名"

# 3) view(dtype) 跨 dtype 保持地址 —— 权重通路的 fp32/int32 技巧
w_i32 = torch.zeros(128, dtype=torch.int32)         # 假想的 weights_buf_local
w_f32 = w_i32.view(torch.float32)
assert w_f32.data_ptr() == w_i32.data_ptr()
w_f32[0] = 1.0                                      # 位模式写入，两侧可见
assert w_i32[0].item() == 0x3F800000                # fp32 1.0 的位模式

# 4) "任何共享 storage 的张量"都继承风险
sub = local[:, :4]
assert sub.untyped_storage().data_ptr() == local.untyped_storage().data_ptr()
print("all alias semantics checks passed")
```

**需要观察的现象**：第 3 步里向 fp32 视图写入 1.0 后，int32 侧读出 `0x3F800000`——这正是 dispatch 权重拷入时 `.view(torch.int32)`（L686）与返回时 `.view(torch.float32)`（L804）能协同工作的原因。

**预期结果**：四组断言全部通过（CPU 可直接运行；若你的环境无 CPU torch 则**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：`hidden_buf` 物理上是 `[R * NvS_padded, H]`，为什么 `hidden_buf_local` 只暴露前 `NvS` 行而不是整个 `NvS_padded` 行的段？

**答案**：尾部 `NvS_padded - NvS` 行是 VMM 粒度对齐补出来的（u2-l2），不属于任何逻辑槽位；dispatch/combine 内核与 `cu_seqlens` 契约都以 `NvS` 为界。暴露它只会让用户读到无定义内容；同时 docstring 也提醒：即使是 `NvS` 行内，也只有 `cu_seqlens` 覆盖的 padded 段有定义（段内 padding 行由零填充 warp 清零，段外行是陈旧数据）。

**练习 2**：`Buffer.destroy()` 之后再访问 `buffer.hidden_nvsh_buffer_view` 会发生什么？

**答案**：抛出断言错误。属性内部先调 `_require_ctx()`（[moonep/api.py:L534-L537](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L534-L537)），而 `destroy()` 已把 `_destroyed` 置 True 并清空 ctx（[moonep/api.py:L559-L580](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L559-L580)）。所以 destroy 前用户手里的旧视图虽仍是 Python 对象，但其底层 VMM 映射已被释放，继续使用是未定义行为——视图的合法生存期天然以 Buffer 为上界。

### 4.4 风险模型：视图不可跨通信调用存活

#### 4.4.1 概念说明

零拷贝的全部风险来自一个事实：**视图别名的是通信基础设施，不是数据**。`hidden_buf_local` 是本 rank 的接收区——全组 R 个 rank 的 dispatch 内核都会经 NVLink 把 token 直写进来（u4-l2 的 S2G 远端直写），本 rank 自己的零填充 warp、combine prologue 也会原地改写它。因此覆盖不受你控制：

1. **覆盖是集合性的**：即使你的进程什么都不做，别的 rank 发起 dispatch 也会写你的接收区；
2. **覆盖是全操作的**：本 Buffer 上的下一次 dispatch（含 plan 复用的 dispatch bwd）、下一次 combine 的拷入/prologue 都会改写；
3. **覆盖不通知**：没有任何标志位告诉你「视图已失效」，读到的旧视图就是**错的但合法的数据**——静默错误。

由此得出视图的生命周期法则：**从获得视图到最后一次读取，中间不得夹杂本 Buffer 上的任何 dispatch/combine**。训练框架的 autograd 天然违反这条法则：前向保存（FFN 输入等）与反向读取之间隔着后续所有 MoE 层的通信，所以「autograd 保存即出错」，这正是 docstring 与 README 都强调必须退回 `zero_copy=False` 的场景。

#### 4.4.2 核心流程

用时间线看一次典型事故（前向第 1 层的视图泄漏到反向）：

```text
t0  dispatch#1(zero_copy=True) → 视图 v 指向接收区，内容 = 第 1 层 token
t1  FFN 原地读写 v；框架 forward 顺手 save_for_backward(v)   ← 已埋雷
t2  combine#1(zero_copy=True) 消费 v                          ← 仍安全
t3  第 2..L 层：dispatch/combine 反复覆写接收区               ← 雷区
t4  backward 开始，autograd 读 v                               ← 读到的是第 L 层数据
t5  权重梯度 silently 错误；无任何异常
```

决策表（何种集成场景用哪档）：

| 场景 | dispatch `zero_copy` | `router_weights_zero_copy` | combine `zero_copy` | 理由 |
| --- | --- | --- | --- | --- |
| 推理（逐层同步消费，无 autograd） | True | True | True | 零边界拷贝，视图当层即弃 |
| 训练 + 原生 autograd 保存激活 | False | False | False | 保存即出错（README L173） |
| 训练 + 自定义 MoE autograd（只存 plan/cu_seqlens，或先 clone） | True | False | 视实现 | 生命周期可控时可部分开启 |

#### 4.4.3 源码精读

**README 的官方警告**（与代码 docstring 同源）：

- [README.md:L154-L173](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L154-L173)：给出 `zero_copy=True` 两侧的完整用法示例（dispatch 返回视图 → FFN 原地写 → combine 断言精确别名），并明确警告：视图别名的是会被下次 dispatch/combine 覆盖的缓冲状态，**不得跨通信调用持有**（autograd 不得为 backward 保存；那种场景必须 `zero_copy=False`）。

**错误路径的官方复现**（传「数值相同的副本」会怎样）：

- [tests/test_e2e.py:L366-L373](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L366-L373)：测试用 `assert_raises_assertion("alias", ...)` 故意向 `combine(zero_copy=True)` 传入先前 `zero_copy=False` 拷出的**数值完全相同**的张量 `h_for_combine`，并断言抛出包含 "alias" 的 `AssertionError`——即 L1009-L1013 的 `data_ptr` 检查把「值对但址不对」的输入拦在内核之外。这是「精确别名」四个字的落点。

**性能路径的实证**（官方基准全开零拷贝）：

- [benchmarks/bench_vs_deepep.py:L260-L283](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L260-L283)：对比基准的前向 dispatch/combine、反向 dispatch/combine 四个象限全部以 `zero_copy=True` 运行（部分入口连带 `router_weights_zero_copy=True`）。基准是无 autograd 的裸内核循环，天然满足生命周期法则——这也反过来示范了零拷贝的目标用户是「自己管理通信与计算重叠」的框架层，而非终端用户。

**异步路径下的补充约束**（衔接 u6-l1）：

- [moonep/api.py:L829-L838](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L829-L838)：`async_finish=True` 时返回值里包含视图张量，它们与所有输入一起被 `record_stream` 到通信流；读视图前必须先 `event.wait()`。对 `zero_copy=True` 的视图，record 的对象正是常驻 NVL 缓冲的切片——存储由 ctx 持有不会被回收，`record_stream` 在这里只是流序保险，不改变 4.4.1 的覆盖风险。

#### 4.4.4 代码实践

**实践目标**：用推理（不运行）写出一个「autograd 保存视图导致静默错误」的最小反例，标出每一步哪个源码行为覆盖了视图。

**操作步骤**：

1. 写出如下伪代码框架（示例代码，非项目源码）：

```python
h_v, w_v, cu, plan = buffer.dispatch(x, w, topk, tpe, zero_copy=True)  # 视图
y_v = my_grouped_ffn(h_v, weights)        # 原地/读视图，输出另分配
out, _, _ = buffer.combine(plan, y_v_into_view, zero_copy=True)
# 事故点：框架 autograd 把 h_v 存进 FFN 的 backward 状态
...  # 后续 MoE 层的 dispatch/combine
out.backward()                            # 读 h_v 时内容早已被覆盖
```

2. 对事故链的每一环标注源码依据：dispatch S2G 远端直写（u4-l2 的 `launch_dispatch`）、零填充 warp 清零（u4-l3）、combine 拷入/prologue 原地累加（L682-L689）。
3. 给出两种修复：(a) dispatch 退回 `zero_copy=False`；(b) FFN 入口先 `h_safe = h_v.clone()` 并只保存 `h_safe`——并说明 (b) 把省下的拷贝又付了一次，仅当框架只需保存小子集时划算。

**需要观察的现象**：修复方案 (a) 与 (b) 的边界拷贝次数各自回到几次。

**预期结果**：(a) 回到两次（全默认路径）；(b) dispatch 零拷贝省一次、clone 又付一次，净省 zero 次、但保留 combine 侧的一次节省。结论：**保存整张 FFN 输入的场景无利可图，直接 `zero_copy=False`**。本实践为推理型，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：rank 0 在自己的 combine 结束后立刻读取自己的 dispatch 视图，期间自己没再调用任何 API，为什么数据仍可能已经变了？

**答案**：接收区被全组共同写入。只要组内其他 rank 已发起了它们的下一次 dispatch，它们的 S2G 就会把 token 远端直写进 rank 0 的 `hidden_buf` 第 0 段——覆盖与本地是否调用 API 无关，这就是「覆盖是集合性的」。

**练习 2**：`plan`（MoonEPCommPlan）也是 dispatch 返回、要跨前向反向保存的对象，它受同样的覆盖风险影响吗？

**答案**：不受。plan 的张量（dst、dup 三件套等）是 `allocate_planning_outputs` 分配的**独立**张量（u3-l1），不在 `hidden_buf`/`meta_buf` 通信区内，内核按 plan 读取而不覆写它（plan 复用路径还专门保证 dedup 结构不被重建，见 u4-l3）。所以跨通信调用保存 plan 是设计内的用法，保存视图不是。

**练习 3**：为什么 MoonEP 用 `assert`（抛 AssertionError）而不是静默回退到拷贝路径来处理「combine 收到非别名的 `zero_copy=True` 输入」？

**答案**：静默回退会掩盖调用方的生命周期 bug——如果调用方误以为自己在零拷贝模式而把同一张量长期持有，回退成拷贝只是这一次侥幸正确，下次仍可能踩覆盖坑。断言把契约违反就地暴露，且 `data_ptr` 检查成本是一次整数比较，可忽略。测试也据此把 AssertionError 当作被测行为（test_e2e.py L366-L373）。

## 5. 综合实践

把本讲全部内容串成一个多卡实验脚本 `zero_copy_probe.py`（基于 u1-l4 的 `my_first_moonep.py` 改造，测试范本为 [tests/test_e2e.py:L343-L373](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L343-L373)）。运行方式（需多 GPU + NVLink）：

```bash
torchrun --nproc_per_node=8 zero_copy_probe.py
```

```python
# 示例代码（实验脚本，非项目源码）
import torch
import torch.distributed as dist
from moonep import Buffer

dist.init_process_group(backend="nccl")
rank, R = dist.get_rank(), dist.get_world_size()
torch.cuda.set_device(rank % torch.cuda.device_count())

S, H, K, E, B = 256, 1024, 4, R * 4, 2
buf = Buffer(S, H, K, E, R, B=B)
g = torch.Generator(device="cuda").manual_seed(rank)
hidden = torch.randn(S, H, dtype=torch.bfloat16, device="cuda", generator=g)
weights = torch.rand(S, K, dtype=torch.float32, device="cuda", generator=g)
topk = torch.randint(0, E, (S, K), dtype=torch.int32, device="cuda", generator=g)
tpe = torch.bincount(topk.flatten(), minlength=E).to(torch.int32)

# --- A. 默认路径：返回独立张量 ---
h0, w0, cu, plan = buf.dispatch(hidden, weights, topk, tpe)
assert h0.data_ptr() != buf.hidden_nvsh_buffer_view.data_ptr(), "A: 默认应拷出"

# --- B. 零拷贝路径：返回精确别名视图 ---
h1, w1, _, _ = buf.dispatch(hidden, weights, topk, tpe,
                            zero_copy=True, router_weights_zero_copy=True)
assert h1.data_ptr() == buf.hidden_nvsh_buffer_view.data_ptr(), "B: hidden 视图"
assert w1.data_ptr() == buf.router_weight_buffer_view.data_ptr(), "B: 权重视图"
assert torch.equal(h1, h0) and torch.equal(w1, w0), "B: 数值须与拷出一致"

# --- C. 复现 combine 的零拷贝检查（与 api.py L1009-L1020 等价）---
def check_zc(t, view, name):
    assert t.is_contiguous() and t.data_ptr() == view.data_ptr(), (
        f"{name} must alias the NVL shard view")
check_zc(h1, buf.hidden_nvsh_buffer_view, "hidden_nvsh")
check_zc(w1, buf.router_weight_buffer_view, "route_weights_nvs")
out1, gw1, _ = buf.combine(plan=plan, hidden_nvsh=h1, route_weights_nvs=w1,
                           zero_copy=True, router_weights_zero_copy=True)
out0, gw0, _ = buf.combine(plan=plan, hidden_nvsh=h0, route_weights_nvs=w0)
assert torch.equal(out0, out1), "C: 两种模式 combine 输出须逐位一致"
assert torch.equal(gw0, gw1) and torch.equal(gw0, weights), "C: 权重收集一致"

# --- D. 错误路径：数值相同但地址不同 → AssertionError("alias") ---
h_fake = h1.clone()
assert torch.equal(h_fake, h1)          # 值相同
try:
    buf.combine(plan=plan, hidden_nvsh=h_fake, zero_copy=True)
    raise RuntimeError("D: 应当断言失败")
except AssertionError as e:
    assert "alias" in str(e), f"D: 意外的断言消息 {e!r}"

# --- E. 覆盖实验：下一次 dispatch 改写旧视图 ---
snapshot = h1.clone()
buf.dispatch(hidden + 1, plan=plan)     # plan 复用路径，新数据写进同一接收区
print(f"[rank {rank}] view content survived re-dispatch: "
      f"{torch.equal(h1, snapshot)}")   # 预期 False：视图内容已被覆盖

buf.destroy()
dist.destroy_process_group()
```

观察清单与预期：

1. **A/B**：`data_ptr` 的两组断言分别给出「独立张量」与「精确别名」的判定，且零拷贝与拷出数值逐位相等（与 test_e2e 的口径一致）。
2. **C**：`check_zc` 是 api.py 断言的逐行复刻；两种模式的 combine 输出（含权重收集）逐位一致——零拷贝只省拷贝、不改语义。
3. **D**：克隆副本触发包含 "alias" 的断言，证明「精确别名」检查的是地址而非数值。
4. **E**：打印 `False`——亲眼看到视图被下一次 dispatch 覆盖，这是 4.4 风险模型的最直接证据。

本实践需 8 卡 NVLink 环境，全部结果**待本地验证**。无 GPU 时可先完成 4.3.4 的 CPU 语义实验与 4.2.4 的真值表练习作为替代。

## 6. 本讲小结

- MoonEP 的通信内核永远以 NVL 对称缓冲为中枢，`zero_copy` 控制的是内核与用户张量之间的两次**边界拷贝**（dispatch 拷出、combine 拷入），每层前向省 \(8\,\mathrm{NvS}H\) 字节 DRAM 流量。
- 开关是**分层**的：`zero_copy` 管 hidden 通路，`router_weights_zero_copy` 独立管权重通路且默认 False——权重小但最易被 autograd 保存，高风险低收益，故要求显式开启。
- dispatch 的零拷贝返回值（及 `hidden_nvsh_buffer_view`/`router_weight_buffer_view` 属性）就是本 rank 接收区的切片：`hidden_buf_local` 的 `[NvS,H]` 行与 `weights_buf_local` 的 fp32 重解释视图。
- combine 用 `data_ptr()` **精确别名**断言把关：数值相同的克隆副本也会被拒（错误消息含 "alias"，test_e2e 已把该错误路径固化为测试）。
- 风险模型三性质：覆盖是**集合性的**（远端 rank 的 dispatch 直写你的接收区）、**全操作的**（任何 dispatch/combine）、**不通知的**（静默错误）；因此视图生命周期不得超过本 Buffer 上的下一次通信调用，autograd 保存即踩雷。
- 适用场景判断：推理/自管生命周期的框架层全开（官方基准即此用法）；原生 autograd 保存激活的训练框架退回 `zero_copy=False`；combine 输出恒为新张量、无此约束。

## 7. 下一步学习建议

本讲补完了 `Buffer` 四入口的最后一个性能旋钮。建议下一步：

1. 学习 **u6-l3 训练全链路：fwd/bwd 四象限**，把本讲的开关决策表放进完整训练步中检验——你会看到 plan 复用路径（combine bwd 的 dispatch、dispatch bwd 的 combine）如何在两次反向通信中再次触碰同一块接收区，从而理解训练框架为何默认全关零拷贝。
2. 精读 [benchmarks/bench_vs_deepep.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py) 的四象限计时结构，对照 u6-l5 的基准方法论，量化零拷贝在真实硬件上的收益（本讲的纸面估算待此验证）。
3. 若你关心二次开发：思考如何在自定义 MoE `autograd.Function` 中只保存 `plan` 与 `cu_seqlens`（它们不受覆盖风险影响，见练习 4.4-2），配合重计算策略安全地启用 `zero_copy=True`——这正是 u6-l6 扩展与二次开发要讨论的框架集成钩子。
