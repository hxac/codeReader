# u15-l7 调试 warp-specialized 内核

## 1. 本讲目标

学完本讲,你应该能够:

1. 按附录给出的八步工作流组织一次调试:先排查环境与编译,再保存并检视生成的 CUDA,最后才回到内核源码,并且**一次只改一处交接**。
2. 为任何一个异步内核写出 roles / storage / handoff / lifetime 四行交接 worksheet,并用它核对生成 CUDA 中的角色守卫、屏障初始化位置与到达数。
3. 记住 TIRx 源码到生成 CUDA 的守卫映射(`wg_id`、`warp_id`、`lane_id`、`elect_sync` 各长成什么样),以及检视时优先搜索的五个字符串。
4. 把一次失败归类为死锁 / 崩溃 / 错误结果 / 正确但慢四类之一,并沿着对应分支的清单逐步排查,而不是盲改算法。

---

## 2. 前置知识

本讲是手册工具链篇的收官,建立在以下已建立的认知之上,不再重复推导:

- **u13-l1 的 Step 7 三角色与四道屏障**:WG1 warp3 做 TMA producer、WG1 warp0 做 MMA consumer、WG0 全体 128 线程做 writeback,由 `tma2mma`(TMABar)、`mma2tma`(TCGen05Bar)、`mma2ld`(TCGen05Bar)、`ld2mma`(MBarrier,128 次到达)连接;`PipelineState` 把 stage 与 phase 捆绑,初始相位「资源起始可用的一端给 1、不可用的一端给 0」;回写分支内不能用 `cta_sync`,须用命名屏障 `warpgroup_sync(10)`。
- **u14-l2 的 FA4 角色与屏障表**:WG3 的三个 warp 分别发起 TMA load、两类 MMA、TMA store,WG0/WG1 各跑一个 Q stage 的 softmax,WG2 做校正与 epilogue;屏障完成条件各不相同——例如 `p_o_rescale` 需要 softmax 组与 WG2 合计 256 次到达。
- **u15-l2 的协作范围纪律**:集体操作(如 `cta_sync`、warpgroup 级 tile 操作)必须被其范围内全部线程一致到达,放进发散分支即死锁;守卫集合必须等于集体操作要求的到达集合。
- **u15-l3 的两级检视**:`kernel.show()` 看 lowering 前的 tile 级 IR,`ex.mod.imports[0].inspect_source()` 看最终生成的 CUDA。

本讲的新名词:

| 名词 | 含义 |
|---|---|
| handoff(交接) | 一个角色把数据或缓冲资源的所有权移交给另一个角色的事件,由屏障、fence 或 drain 保障 |
| worksheet(工作表) | 调试前手写的 roles/storage/handoff/lifetime 四行表,是核对生成代码的模型 |
| context poisoning(上下文污染) | 一次非法访存后 CUDA context 受损,后续无关调用也持续失败的状态 |
| XID | NVIDIA 驱动级错误码,通常伴随不可恢复的 GPU 故障 |
| drain(排空) | 等待一批异步操作真正结束,如 TMA store 的 `wait_group(0)` |

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [appendix/debugging_warp_specialized.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md) | 本讲主角:调试方法论附录——环境检查、八步工作流、交接 worksheet、生成代码映射表、Step 7 参考骨架、四类症状清单 |
| [chapter_gemm_advanced/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md) | Step 7 内核完整源码与四道屏障的定义,是本讲 worksheet 的第一个实例 |
| [chapter_flash_attention/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md) | FA4 的 warp 角色表与屏障完成条件表,是 worksheet 的第二个实例(补充引用) |

---

## 4. 核心概念与源码讲解

### 4.1 调试工作流:先环境、再编译、再内核

#### 4.1.1 概念说明

warp-specialized 内核把 TMA 加载、tcgen05 MMA、TMEM/SMEM 回写重叠执行,一条 Python 源码行会被展开成角色守卫、屏障到达与异步指令的组合。当这样的内核出错时,直觉反应往往是「重写内核」,而附录的第一条纪律恰好相反:**不要先重写内核**。

附录在开篇给出了一个收敛性的判断——排除环境与编译问题之后,这类内核的运行期故障通常都归结为「一次断裂的交接」(a broken handoff),只有五种形态:

1. 屏障未初始化;
2. 到达数(arrival count)不对;
3. 集体操作被藏进了角色守卫里(到达集合不齐);
4. 屏障相位过期(stale phase);
5. 存储在生产者写入可见之前就被复用。

见 [appendix/debugging_warp_specialized.md:L4-L6](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L4-L6)。这五种形态全部是**交接层**的问题,不是算法层的问题——这就是「先别重写内核」的根据:你大概率不需要新算法,需要的是找出哪一次交接断了。

「先环境」的理由在 u1-l3 已建立过一半:书中内核依赖 `tvm.tirx` 模块与 Blackwell(`sm_100a`)GPU。如果 Python 导入的是一个过期的 TVM 检出,或者 GPU 不是 Blackwell 级别,那么任何内核改动都无济于事。

#### 4.1.2 核心流程

附录把整个调试过程排成八步([appendix/debugging_warp_specialized.md:L19-L28](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L19-L28)),用伪代码表达:

```text
01  在仍能复现失败的最小 shape 上复现
    └─ 若是非法访存:先重启 Python 再跑下一次
02  若编译失败:先查 API 版本 / target / dispatch= / buffer scope
    (不要去读运行期同步代码)
03  保存 inspect_source("cuda") 输出
    └─ 先在里面搜:角色守卫、mbarrier_init、tcgen05、
       cp.async.bulk.tensor、__syncthreads(),再回头读 Python
04  为失败的内核路径写 roles/storage/handoff/lifetime 四行表
05  拿生成 CUDA 对照该表:
    屏障 init 是否在角色分支之前 / 是否有预期的 TMA producer、
    MMA 发起者、回写组 / warpgroup 分支里有没有 CTA 级集体操作
06  把本次运行归类:死锁 / 崩溃 / 错误结果 / 正确但慢,走对应小节
07  一次只改一处交接:init 数、arrive/wait 相位、角色守卫、
    fence、TMA store 排空、TMEM alloc/dealloc、tile scheduler 前进
08  测性能之前先重跑正确性
```

环境自检是第 0 步,两条命令直接来自附录([appendix/debugging_warp_specialized.md:L12-L15](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L12-L15)):

```bash
python -c "import tvm, tvm.tirx; print(tvm.__file__, tvm.__version__)"
python -c "import torch; print(torch.cuda.get_device_name(), torch.cuda.get_device_capability())"
```

第一条验证加载的 TVM 路径与版本(u1-l3 讲过用 `tvm.__file__` 排查旧检出污染),第二条验证 GPU 型号与算力是否为 Blackwell。通过之后,先跑该内核的最小正确性检查(如 `run_correctness()`),再看性能——这与 u15-l4 的「正确性先行」纪律同源。

编译期失败优先于运行期调试,附录给出四条对照([appendix/debugging_warp_specialized.md:L51-L61](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L51-L61)):

| 症状 | 可能原因 | 先查什么 |
|---|---|---|
| 未知 TIRx API 或属性错误 | 安装的 wheel 与教程代码不匹配 | 打印 `tvm.__file__` / `tvm.__version__`,对照语言参考核对 API 名 |
| 不支持的 `dispatch=` | 所选 target 或原语不支持该路径 | 查 `dispatch` 参数与 target 能力;本教程的 `tcgen05` 路径需要 Blackwell |
| buffer scope 不匹配 | buffer 走了错误的硬件路径 | 查 worksheet 的 storage 行:TMEM 必须经 `tcgen05` 访问,TMA 操作数需要兼容的 GMEM/SMEM 布局 |
| 编译通过但生成 CUDA 缺少预期路径 | dispatch 没有按预期 lower | 在生成 CUDA 里搜 `tcgen05` 与 `cp.async.bulk.tensor`,再考虑改算法 |

#### 4.1.3 源码精读

「先环境后内核」一节的原文是:

> Do not start by rewriting the kernel. First verify the environment and reproduce the failure with the smallest correctness test, then inspect the generated CUDA.

见 [appendix/debugging_warp_specialized.md:L8-L17](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L8-L17)。这一节同时强调:这些内核以 Blackwell(`sm_100a`)为目标,环境不对就先修环境。

八步工作流的原文见 [appendix/debugging_warp_specialized.md:L19-L28](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L19-L28),其中第 7 步值得逐项读懂——「一次只改一处交接」枚举了七类可改对象:`init` 数、`arrive`/`wait` 相位、角色守卫、fence、TMA store 排空(`commit_group` + `wait_group(0)`)、TMEM `alloc`/`dealloc`、tile scheduler 前进(`next_tile()`)。这七类恰好就是 4.2 节 worksheet 中 handoff 与 lifetime 两行会记录的东西;换言之,**工作流的第 7 步就是「沿着 worksheet 改」**。

注意编译失败表中第四行的方向性:它把「生成 CUDA 缺少预期路径」列为**编译期**问题——`dispatch` 参数写错(比如忘了 `dispatch="tcgen05"`)时编译照样通过,只是静默走了别的路径。这类问题在 u9-l3 讲过:dispatch 只在存在多种硬件实现时有意义,写错了不会报错,只会走错路。

#### 4.1.4 代码实践

**实践目标**:建立自己机器上的调试前置检查清单,并验证「环境不对时内核改动无效」这条纪律。

**操作步骤**:

1. 运行上面两条环境命令,记录四项输出:`tvm.__file__`、`tvm.__version__`、GPU 名称、算力(形如 `(10, 0)` 或 `(9, 0)`)。
2. 把四项输出与书中要求对照:版本应为 `apache-tvm==0.26.0`(u1-l3),算力应为 Blackwell 级(支持 `tcgen05`/TMEM)。
3. 若无 Blackwell GPU,写一份「环境限制清单」,注明本机无法验证运行期症状,后续实践改为源码推演 + 生成代码检视(若可编译)。

**需要观察的现象**:`tvm.__file__` 指向的路径是否是你安装的 wheel(而非某个源码检出);算力是否 ≥ sm_100a。

**预期结果**:环境清单四项全部记录在案。无 GPU 时此实践即为「推演模式的基线声明」。运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**:为什么「内核挂死」要先查环境而不是先查屏障?

**参考答案**:因为环境错误(旧 TVM 检出、非 Blackwell GPU)会让任何内核改动都无效——你可能在调试一个根本不该由内核负责的问题。附录把环境检查排在八步工作流之前,只有环境排除后才把运行期故障收敛到五种交接形态。

**练习 2**:编译通过、运行结果错误。按下表哪一行先排查「生成 CUDA 里没有 `cp.async.bulk.tensor`」?为什么?

**参考答案**:按编译失败表的第四行(「编译通过但生成 CUDA 缺少预期路径」)排查——dispatch 没有按预期 lower(如 `dispatch` 参数写错或 target 能力不符)。先在生成 CUDA 里确认路径,再考虑改算法;这正是「症状映射表」中「正确但慢」分支的第一条线索的同源逻辑。

---

### 4.2 交接 worksheet:把内核抽象成四行表

#### 4.2.1 概念说明

worksheet 是调试的核心工具:**在改任何代码之前**,把异步内核抽象成一张四行表,每行回答一个问题([appendix/debugging_warp_specialized.md:L30-L39](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L30-L39)):

| 行 | 写什么 |
|---|---|
| **Roles**(角色) | 发起每个异步操作的精确线程范围:哪些线程、warp、warpgroup 或 CTA |
| **Storage**(存储) | 每一步时每块 tile 的存活性位置:GMEM、SMEM、TMEM 还是寄存器 |
| **Handoff**(交接) | 生产者、消费者、信号对象、到达数、相位,以及让数据可见所需的 fence 或 drain |
| **Lifetime**(生命周期) | 每个存储槽最早何时可被复用、读回或释放 |

这张表为什么有用?因为它把「我对内核的期望」显式写下来了。接下来核对生成 CUDA 时,你手里有了一份可逐条比对的规格——而不是凭印象猜哪条屏障可能有问题。附录明确说,同一张 worksheet 既适用于 GEMM 的 TMA → MMA → 回写流水线,也适用于 FA4 中 QKᵀ MMA、softmax、PV MMA 与校正之间的交接(见 [appendix/debugging_warp_specialized.md:L49](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L49)):两边的差异只是角色与屏障的名字,方法论完全一致。

#### 4.2.2 核心流程

写好 worksheet 后,按五条检查把生成 CUDA 与表逐条对照([appendix/debugging_warp_specialized.md:L41-L47](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L41-L47)):

```text
核对 1:角色守卫与 Roles 行一致
核对 2:屏障 init 出现在被守卫的角色分支之前
核对 3:集体操作没有被 lane/warp/warpgroup 守卫意外缩小到达集合
核对 4:arrive/wait 的相位与 Handoff 行一致
核对 5:TMA store 的完成被等待、TMEM 被 dealloc、
       SMEM 只在 Lifetime 行允许的时机被复用
```

这五条核对正对应 u15-l2 的「守卫集合 = 集体操作要求的到达集合」纪律:核对 2 与核对 3 是它的静态形式,核对 4 是它的相位形式,核对 5 是 lifetime 的资源形式。

#### 4.2.3 源码精读

**实例一:Step 7 的完整 worksheet。** 以下表格的每一项都能在 [chapter_gemm_advanced/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md) 中找到出处。

Roles 行——三个并发角色加两个资源管理角色:

| 角色 | 线程范围 | 源码依据 |
|---|---|---|
| TMA producer | WG1 warp 3,`elect_sync` 选出的单线程 | 角色表 [chapter_gemm_advanced/index.md:L50-L56](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L50-L56),守卫 `if wg_id == 1: if warp_id == 3:` 加 `T.filter(lane_id, T.ptx.elect_sync())` 见 [L205-L229](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L205-L229) |
| MMA consumer | WG1 warp 0,`elect_sync` 选出的单线程 | [chapter_gemm_advanced/index.md:L231-L254](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L231-L254) |
| Writeback | WG0 全体 4 warp、128 线程 | [chapter_gemm_advanced/index.md:L259-L294](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L259-L294) |
| 屏障 init / TMEM 管理 | init 由 CTA 线程 0;`tcgen05.alloc` 由 WG0 warp0 全体 lane;dealloc 由 warp0 | init 调用 [L176-L179](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L176-L179),alloc 守卫 [L182-L188](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L182-L188),cleanup [L296-L300](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L296-L300) |

Storage 行——一块输出 tile 的一生:

```text
A/B tile:  GMEM --TMA--> Asmem/Bsmem[stage](SMEM,128B swizzle)
                              --tcgen05 描述符读--> Tensor Core
累加器:    TMEM tmem[:, :128](每个输出 tile 一份,fp32)
结果:      TMEM --tcgen05.ld--> 寄存器 reg(每线程 128 个 fp32)
           --Tx.cast--> reg_f16 --> Dsmem(SMEM) --TMA store--> GMEM 的 D
```

Handoff 行——四道屏障逐条记录(屏障定义表见 [chapter_gemm_advanced/index.md:L60-L71](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L60-L71)):

| 屏障 | 类型 / init 数 | 生产者(到达方式) | 消费者(等待方) |
|---|---|---|---|
| `tma2mma[stage]` | TMABar / 1 | producer 单线程 `arrive(stage, 32768)`,TMA 引擎扣减字节 | MMA consumer 在发 MMA 前 `wait` |
| `mma2tma[stage]` | TCGen05Bar / 1 | MMA 单线程 `tcgen05.commit`,硬件在 MMA 完成后到达 | TMA producer 在覆写该 stage 前 `wait` |
| `mma2ld` | TCGen05Bar / 1 | MMA 在 K 循环结束后 `arrive` | WG0 回写组在读 TMEM 前 `wait` |
| `ld2mma` | MBarrier / 128 | WG0 全部 128 线程各 `arrive` 一次 | MMA consumer 在下一个输出 tile 前 `wait` |

其中 32768 = `(BLK_M*BLK_K + BLK_N*BLK_K) * F16_SIZE`,即 `(128*64+128*64)*2`,出自 producer 侧的 `tma2mma.arrive` 调用([chapter_gemm_advanced/index.md:L226-L227](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L226-L227));`ld2mma.init(128)` 的注释「all 128 Warpgroup 0 threads arrive」见 [L179](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L179)。附录的死锁小节有一张同源的三行屏障账本表(TMABar / TCGen05Bar / MBarrier 各自的 init 数与到达方式),见 [appendix/debugging_warp_specialized.md:L155-L159](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L155-L159)。

Lifetime 行——三个缓冲的最早可复用点,正文原话是:

> TMA may overwrite an `Asmem` or `Bsmem` stage only after `mma2tma` completes. MMA may overwrite the TMEM accumulator only after `ld2mma` completes. A later writeback may reuse `Dsmem` only after the previous TMA store finishes.

见 [chapter_gemm_advanced/index.md:L307-L311](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L307-L311)。同一节还给出手工跟踪的建议:让一个 K tile 依次走过 `tma2mma`、`mma2tma`、`mma2ld`、`ld2mma`,对每道屏障写清谁等待、谁到达、哪些数据可读、哪个缓冲可复用。

**实例二:FA4 的 handoff 行摘录。** FA4 角色更多(WG3 三个 warp + WG0/WG1 softmax + WG2 校正,角色表见 [chapter_flash_attention/index.md:L210-L219](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L210-L219)),屏障完成条件表见 [chapter_flash_attention/index.md:L296-L309](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L296-L309)。其中 `p_o_rescale` 一行最能体现 worksheet 的价值——它写的是「128 个 softmax 线程 + 128 个 WG2 线程,合计 256 次到达」([L303](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L303)):一次 `wait` 同时证明 P 的前 `K_SPLIT` 列就绪与 O 槽可用。如果只看代码不看表,很容易把 256 拆错成两个 128 而怀疑到达数写错了。另外 FA4 的寄存器分配也属于 Roles 行的扩展:`setmaxnreg` 按角色把每线程上限调为 200/200/64/48(见 [chapter_flash_attention/index.md:L240-L258](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L240-L258))。

#### 4.2.4 代码实践

**实践目标**:亲手完成 Step 7 的「跟踪一个 K tile」练习,把 Handoff 行从静态表格变成时序记录。

**操作步骤**:

1. 以 4.2.3 的四行表为底稿,取 `k=0`(第一个 K tile,此时所有屏障都在初始相位)。
2. 按执行顺序写出六条事件:TMA producer 的 `mma2tma.wait` → `tma_load` → `tma2mma.arrive`;MMA consumer 的 `tma2mma.wait` → `Tx.gemm_async`(accum=False)→ `mma2tma.arrive`。
3. 对每条事件标注:哪个角色执行、作用于哪道屏障的哪个 stage、此时该屏障的相位是几。
4. 回答:如果 `mma2tma` 的第一次 `wait` 立即通过了,是哪一端的初始相位设置使然?

**需要观察的现象**:`tma_ps` 初始 `phase=1` 使 producer 的第一次 `mma2tma.wait(0, 1)` 立即通过(缓冲起始为空,本就该放行);`mma_ps` 初始 `phase=0` 使 consumer 的第一次 `tma2mma.wait(0, 0)` 阻塞(数据还没到)。两处初始化见 [chapter_gemm_advanced/index.md:L208](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L208) 与 [L233](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L233)。

**预期结果**:得到一张「事件 / 角色 / 屏障 / stage / 相位」五列时序表,能清楚指出 producer 与 consumer 的第一次 wait 为何一个通过一个阻塞。纯源码推演即可完成,无需 GPU。

#### 4.2.5 小练习与答案

**练习 1**:Handoff 行为什么要记录「fence 或 drain」,而不是只记录屏障?

**参考答案**:屏障只报告完成事件;数据是否**可见**还需要额外的顺序保障。例如 Step 7 的回写路径中,`mma2ld.wait` 只确认 MMA 完成,其后还需要 `T.ptx.tcgen05.fence.after_thread_sync()` 才能保证 `tcgen05.ld` 排在跨线程完成通知之后;TMA store 之前需要 `fence.proxy_async("shared::cta")` 让引擎看到线程的 SMEM 写入,之后需要 `commit_group` + `wait_group(0)` 排空。漏掉任何一处,屏障全对也可能读到旧数据。

**练习 2**:FA4 的 `p_o_rescale` 到达数是 256,而 Step 7 的 `ld2mma` 是 128。到达数由什么决定?

**参考答案**:由「屏障类型 + 参与交接的线程范围」共同决定。`ld2mma` 是普通 MBarrier,由 WG0 全部 128 线程各到达一次;`p_o_rescale` 也是普通 MBarrier,但它的完成条件是 softmax 组(128)与 WG2(128)两组线程都报到达,合计 256——一次等待同时证明 P 就绪与 O 可用。附录死锁小节的屏障账本表概括了三类屏障的到达规则(见 [appendix/debugging_warp_specialized.md:L155-L159](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L155-L159))。

---

### 4.3 生成代码检视:inspect_source 与角色守卫的映射

#### 4.3.1 概念说明

TIRx 源码(`if wg_id == 1:`)与生成 CUDA(`if ((warp_id_in_cta >> 2) == 1)`)之间隔着一层 lower。当行为可疑时,只读 Python 源码可能永远找不到问题——因为真正执行的是生成代码。所以工作流第 3 步要求:把 `inspect_source("cuda")` 的输出**保存成文件**,先在里面搜索关键结构,再回头读 Python。

保存成文件有两个好处:可以反复搜索,可以在改代码前后 diff。附录给出的保存方法是([appendix/debugging_warp_specialized.md:L62-L73](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L62-L73)):

```python
from pathlib import Path

cuda_source = ex.mod.imports[0].inspect_source("cuda")
Path("artifacts").mkdir(exist_ok=True)
Path("artifacts/my_kernel.cu").write_text(cuda_source, encoding="utf-8")
print(cuda_source)
```

#### 4.3.2 核心流程

检视分两步:**先认守卫,再搜字符串**。

第一步,记住 TIRx 构造到生成 CUDA 的映射表([appendix/debugging_warp_specialized.md:L77-L85](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L77-L85)):

| TIRx 源码 | 生成 CUDA |
|---|---|
| `wg_id == 0` | `(warp_id_in_cta >> 2) == 0` |
| `wg_id == 1` | `(warp_id_in_cta >> 2) == 1` |
| `warp_id == 0` | `(warp_id_in_cta & 3) == 0` |
| `warp_id == 3` | `(warp_id_in_cta & 3) == 3` |
| `lane_id == 0` | `(((int)threadIdx.x) % 32) == 0` |
| `.init()` 内部守卫 | `((int)threadIdx.x) < 1`(仅 CTA 线程 0) |
| `elect_sync()` | `tvm_builtin_elect_one_sync_op()` |

注意 `wg_id` 与 `warp_id` 的生成式来自两个不同的线程坐标声明:`T.warpgroup_id` 除以 4 得 warpgroup 编号,`T.warp_id_in_wg` 取模 4 得组内 warp 编号——正是 u13-l1 讲过的两级坐标。

第二步,通读内核之前先搜五个字符串([appendix/debugging_warp_specialized.md:L87-L95](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L87-L95)):

| 生成 CUDA 中的字符串 | 检查什么 |
|---|---|
| `if (threadIdx.x < 1)` | 单 CTA 线程守卫,通常是屏障初始化 |
| `mbarrier_init` | 屏障初始化存在,且出现在角色分支**之前** |
| `tcgen05` | Tensor Core 路径确实生成了 |
| `cp.async.bulk.tensor` | 拷贝确实 lower 成了 TMA |
| `__syncthreads();` | 由 `T.cuda.cta_sync()` 生成;**不得出现在 `wg_id` 分支内部** |

#### 4.3.3 源码精读

检视的对照物是附录给出的 **Step 7 参考骨架**——一个正确编译的 Step 7 内核应有的顶层形状([appendix/debugging_warp_specialized.md:L97-L123](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L97-L123)):

```text
// (1) 屏障 init:顶层,仅 CTA 线程 0
if (threadIdx.x < 1) {
  mbarrier_init(tma2mma[0..1], 1);
  mbarrier_init(mma2tma[0..1], 1);
  mbarrier_init(mma2ld, 1);
  mbarrier_init(ld2mma, 128);   // WG0 全部 128 线程到达
}

// (2) TMEM alloc:WG0 warp0,发起 warp 的全部 lane
if (wg_id == 0 && warp_id == 0) tcgen05_alloc(..., 512);

// (3) fence + __syncthreads,然后相位初始化:producer=1, consumer=0

// (4) warp 特化循环
if (wg_id == 1 && warp_id == 3 && elect_sync) { /* TMA */ while(valid){...next_tile();} }
if (wg_id == 1 && warp_id == 0 && elect_sync) { /* MMA */ while(valid){...next_tile();} }
if (wg_id == 0)                                { /* WB  */ while(valid){...next_tile();} }

// (5) 清理:发起 warp,无 lane 守卫
__syncthreads();
if (warp_id == 0) { tcgen05_relinquish_alloc_permit(); tcgen05_dealloc(..., 512); }
```

(骨架为附录原文的示意代码,守卫以角色名书写;在生成 CUDA 中应搜索映射表里的对应表达式。)

改算法之前先核对四件事([appendix/debugging_warp_specialized.md:L125-L130](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L125-L130)):

- 屏障 init 在顶层,不在 `wg_id` 守卫内;
- `tcgen05_alloc` 与 `tcgen05_dealloc` 有 warp 守卫但没有 lane 守卫(发起 warp 全体 lane 参与);
- TMA 与 MMA 两个循环都迭代 `K_TILES` 次;
- 相位初始化为 producer=`1`、consumer=`0`。

这四条核对与 4.2 节 worksheet 的五条核对互为表里:worksheet 是「应然」,骨架是「实然」——生成代码长这样,才说明 lower 没有偏离你的模型。骨架中的五个段落也都能在 Step 7 的 TIRx 源码里找到对应:屏障 init 对应 [chapter_gemm_advanced/index.md:L176-L180](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L176-L180),TMEM alloc 对应 [L182-L188](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L182-L188),三角色循环对应 [L205-L294](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L205-L294),清理对应 [L296-L300](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L296-L300)。

#### 4.3.4 代码实践

**实践目标**:把「检视生成代码」变成一条可重复的命令流水线。

**操作步骤**:

1. 在有 Blackwell GPU 的环境下编译 Step 7(u9-l2 的回路:`tvm.compile(tir_pipeline="tirx")`),用上面的脚本把 CUDA 源保存为 `artifacts/hgemm_v7.cu`。
2. 对五个关键字符串分别统计出现次数并记录行号,例如:

   ```bash
   grep -n "mbarrier_init" artifacts/hgemm_v7.cu
   grep -n "cp.async.bulk.tensor" artifacts/hgemm_v7.cu
   grep -n "__syncthreads();" artifacts/hgemm_v7.cu
   grep -n "tcgen05" artifacts/hgemm_v7.cu | head -20
   grep -n "threadIdx.x < 1" artifacts/hgemm_v7.cu
   ```

3. 核对三件事:`mbarrier_init` 是否全部出现在第一个 `wg_id` 分支之前;`__syncthreads();` 是否只出现在顶层与清理段(不在 `wg_id` 分支内);`ld2mma` 的 init 数是否为 128。
4. 故意做一次「改前/改后」对照:把 Step 7 源码里 `ld2mma.init(128)` 改成 `ld2mma.init(64)`(仅用于观察,看完改回),重新保存为 `artifacts/hgemm_v7_bug.cu`,diff 两个文件确认只有这一处数字变化。

**需要观察的现象**:diff 输出应当只有 `mbarrier_init(..., 128)` 到 `(..., 64)` 一行变化;此时若运行内核,`ld2mma` 的 `wait` 永远等不齐 128 次到达,内核挂死——这是 4.4 节死锁分支第一条(到达数与 init 数不匹配)的活标本。

**预期结果**:得到一份「字符串 / 出现次数 / 行号」清单,以及一次最小 diff 的经验。无 Blackwell GPU 时,第 1–3 步是否可执行取决于能否只做 CUDA 代码生成,**待本地验证**;不可执行时,以本节映射表为依据,对 Step 7 的 TIRx 源码做「守卫 → 生成表达式」的纸面翻译练习代替。

#### 4.3.5 小练习与答案

**练习 1**:为什么 `tcgen05_alloc` 的守卫「有 warp 但没有 lane」?加了 `lane_id == 0` 会怎样?

**参考答案**:`tcgen05.alloc` 是 warp 集体指令,发起 warp 的 32 个 lane 必须一起执行(这与 u7-l3 讲的分配生命周期一致)。加 `lane_id == 0` 守卫后只有一个线程执行,属未定义行为——附录把它列在「崩溃与上下文污染」一节([appendix/debugging_warp_specialized.md:L171-L178](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L171-L178))。

**练习 2**:生成 CUDA 里某个 `__syncthreads();` 出现在 `(warp_id_in_cta >> 2) == 0` 的分支内部。这对应 TIRx 源码里的什么错误?后果是什么?

**参考答案**:对应把 `T.cuda.cta_sync()` 写进了 `if wg_id == 0:` 分支。`__syncthreads()` 要求 CTA 全部 256(或 512)线程到达,而 WG1 正在执行 producer/MMA 路径永远到不了这里,于是 WG0 集体卡死——内核死锁。正确做法是用命名屏障 `T.cuda.warpgroup_sync(10)`(即 `bar.sync 10, 128`),这正是 u13-l1 与 u15-l2 讲过的规则,附录死锁小节也单列了这一条。

---

### 4.4 症状映射表:四类症状的排查路径

#### 4.4.1 概念说明

环境与编译排除、worksheet 写好、生成代码核对完之后,剩下的运行期故障按**症状**分四类:死锁(deadlock)、崩溃(crash)、错误结果(wrong result)、正确但慢(correct but slow)。附录的提醒是:症状是线索(clue),不是最终诊断——同样的「错误结果」可能来自相位错、tile 索引错或角色所有权错,要靠模式(pattern)进一步分类。

四类症状的入口表([appendix/debugging_warp_specialized.md:L132-L143](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L132-L143)):

| 线索 | 可能原因 | 先查什么 |
|---|---|---|
| 内核挂死,随后运行时报未指明的 launch 失败 | 死锁 | 屏障 init 位置、到达数、`cta_sync()` 位置、`next_tile()` 参与度 |
| 非法访存、XID,或之后无关的 CUDA 调用也开始失败 | 崩溃 / 上下文污染 | 重启 Python;再查指针范围、存储生命周期、集体操作参与度 |
| 错误的行以 128 行或 tile 尺寸的条带出现 | 同步竞争或 tile 索引不匹配 | 生产者/消费者相位、scheduler 前进、每个 warpgroup 拥有哪条行带 |
| `NaN` 或明显非法的值 | 描述符、操作数设置或未初始化累加 | SMEM/TMEM 描述符设置、swizzle/布局、累加器初始化 |
| 有限但有模式的错误值 | 旧数据或部分可见的数据 | 缺 fence、缺 TMA store 排空、或存储早于 lifetime 允许被复用 |
| 输出正确但没有预期加速 | dispatch 或资源问题 | 生成 CUDA 路径、流水线深度、occupancy、寄存器溢出 |

特别注意第一、二行的运行时行为差异:**挂死**等很久才报错,**非法访存**则会毒化 context——附录专门用一节说明,非法访存之后即使无关调用(如 `torch.randn`)也会持续失败,必须重启 Python 进程再测下一个修复,否则「你调试的是上一次崩溃,而不是当前代码」(见 [appendix/debugging_warp_specialized.md:L145-L147](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L145-L147))。

#### 4.4.2 核心流程

四类症状的排查决策树:

```text
症状分类
├─ 死锁(挂死 → launch 失败)
│   按序查六条:
│   1 到达数 ≠ init 数(对照屏障账本表)
│   2 屏障 init 嵌进了 wg_id 守卫
│     (init 降级为 threadIdx.x<1,而 CTA 线程 0 属于 WG0)
│   3 cta_sync 出现在 warpgroup 分支内
│   4 next_tile() 被某些 consumer-warpgroup 线程跳过
│     (scheduler 跟踪每线程状态,跳过者永久循环)
│   5 TMA 与 MMA 的 K-tile 数不一致(K_TILES-1 vs K_TILES)
│     (相位漂移,第二个外层 tile 死锁)
│   6 PipelineState 初始相位错(两端同相 → 第一次交接即死锁)
│
├─ 崩溃 / 上下文污染
│   先重启 Python,再查四条:
│   1 pool.commit() 之后还有 pool.alloc
│   2 tcgen05.alloc/dealloc 带 lane 守卫(未定义行为)
│   3 tcgen05.dealloc 前缺 cta_sync(回写还在读 TMEM)
│   4 GMEM/SMEM 越界(缩到一个 tile,查 m_idx/n_idx,
│     确认形状是 tile/cluster tile 的整数倍)
│
├─ 错误结果(先按模式分类,再对号入座)
│   · tcgen05.commit 不在 elect_sync 内
│     (32 线程各建 commit 组,31 个空组立即到达屏障,
│      TMA 在 MMA 读之前覆写 SMEM)
│   · TMA store 前缺 fence.proxy_async("shared::cta")
│   · TMA store 后缺 commit_group + wait_group(0)
│   · persistent 内核在小尺寸(如 1024x1024)间歇性失败
│     (长 K 循环掩盖竞争;重查 tile 间相位复位与 store 排空)
│   · MMA 完成与 TMEM load 之间缺 fence.after_thread_sync()
│
└─ 正确但慢
    · 生成 CUDA 无 cp.async.bulk.tensor → 查 dispatch="tma_auto"
    · 生成 CUDA 无 tcgen05 → 查 dispatch="tcgen05"
    · TMA 与 MMA 不重叠 → 检视生成代码中 wait/arrive/advance 的顺序
    · 小形状好、大形状差 → 寄存器溢出/occupancy/staging 压力,
      查资源报告,减 tile、分块回写或降流水线深度
```

死锁分支第 1 条的屏障账本表(到达数到底该是几)见 [appendix/debugging_warp_specialized.md:L155-L159](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L155-L159);崩溃四条见 [L171-L178](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L171-L178);错误结果五条见 [L180-L188](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L180-L188);正确但慢四条见 [L190-L199](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L190-L199)。

#### 4.4.3 源码精读

几个值得精读的条目:

**「commit 不在 elect_sync 内」为什么产生错误结果而不是死锁。** 附录的解释是:32 个线程各自创建 commit 组,其中 31 个空组会**立即**到达屏障——屏障因此提前放行,TMA 在 MMA 还没读 SMEM 时就覆写了它(见 [appendix/debugging_warp_specialized.md:L184](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L184))。对比 u8-l1 的结论「expect_tx 登记过小导致静默读错数据」:屏障账本类错误从来不是「报不报错」的问题,而是「提前放行(错果)」还是「永不归零(挂死)」的问题,方向取决于多登还是少登。

**「缺 `fence.after_thread_sync()`」的精确语义。** 附录强调:`mma2ld` 的 wait 只确认 MMA 完成,但回写线程在发 `tcgen05.ld` 之前还需要 `T.ptx.tcgen05.fence.after_thread_sync()` 来建立「新线程的 TMEM load 排在跨线程完成通知之后」的顺序;Steps 7–9 都把这条 fence 放在 `mma2ld.wait` 紧后。同时它**不**等待 TMA load,也**不**让普通线程写对 TMA 引擎可见——那些交接各有自己的 mbarrier 与 proxy-fence 协议(见 [appendix/debugging_warp_specialized.md:L188](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L188))。在 Step 7 源码中,这条 fence 出现在回写循环 `mma2ld.wait` 之后的第三行([chapter_gemm_advanced/index.md:L265-L267](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L265-L267))。

**崩溃第一条的分配顺序。** `pool.alloc` 不能出现在 `pool.commit()` 之后,因为屏障包装类(TMABar 等)内部会调用 `alloc`。正确顺序是:`tmem_addr → 各屏障包装 → move_base_to(1024) → Asmem/Bsmem/Dsmem → commit()`,对应 Step 7 源码 [chapter_gemm_advanced/index.md:L164-L180](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L164-L180)——这也解释了为什么控制对象要占低地址、操作数要 `move_base_to(1024)` 挪开(u11-l2)。

**「正确但慢」与剖析工具的衔接。** 该分支的四条线索最终都要回到 u15-l5 的剖析流水线确认(如用 NCU 查寄存器溢出与 occupancy),而 dispatch 两条则是 4.1 节编译失败表第四行的运行时镜像:同样的「生成 CUDA 缺路径」,出现在编译期是排错,出现在测速期是排慢。

若以上检查全部穷尽仍无法解释,附录最后一节给出提交 issue 的清单:环境输出、最小复现 shape、症状分类、最小内核或 notebook 单元加正确性检查、保存的 `inspect_source("cuda")` 输出或最能说明问题的片段,提交到 Apache TVM 仓库(见 [appendix/debugging_warp_specialized.md:L201-L209](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L201-L209))。

#### 4.4.4 代码实践

**实践目标**:为「死锁」症状手工走一遍排查清单,并与 u13-l1 的章末练习 1 互相印证。

**操作步骤**:

1. 取 Step 7 源码,把 MMA consumer 的 `mma_ps` 初始相位从 `phase=0` 改成 `phase=1`([chapter_gemm_advanced/index.md:L233](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L233) 一行)。
2. 不运行,只推演:MMA 的第一次 `tma2mma.wait(0, 1)` 会怎样?(提示:屏障初始相位为 0,`try_wait(1)` 在屏障离开相位 1 后才返回——u8-l2 的相位判据。)
3. 按死锁清单六条逐条打勾,确认本故障属于第 6 条(PipelineState 初始相位错),并写出推演链:consumer 提前/推迟放行 → 读到的 SMEM 是什么 → 下游哪道屏障随后永不归零。
4. 对照 u13-l1 章末练习 1(「两端初始相位都设 0 会怎样」,见 [chapter_gemm_advanced/index.md:L905-L908](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L905-L908)),画两张时序图,分别标出死锁发生的第一次交接。

**需要观察的现象**:两端初始相位「同为 1」与「同为 0」死锁的位置不同——前者是 consumer 永久阻塞在第一次 `tma2mma.wait`,后者按附录死锁第 6 条的描述「第一次交接立即死锁」。

**预期结果**:两张死锁时序图 + 一条「改了哪一处交接(第 7 步纪律:一次只改 init 数/相位/守卫/fence/drain/alloc/scheduler 之一)」的说明。纯推演完成,无需 GPU。

#### 4.4.5 小练习与答案

**练习 1**:「persistent 内核在 1024x1024 这样的小尺寸时间歇性失败,大尺寸反而稳定」——为什么大尺寸会掩盖竞争?

**参考答案**:大尺寸意味着更长的 K 循环与更多 tile,某些相位错位或缺失的 store 排空在长时间尺度下被后续等待「碰巧」补救,竞争难以触发;小尺寸下时序窗变窄,竞争暴露。附录给出的排查方向是重查 tile 之间的相位复位(每 tile 屏障使用次数须使奇偶归零,u12-l3 的约束)与 TMA store 的 commit/wait(见 [appendix/debugging_warp_specialized.md:L187](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L187))。

**练习 2**:错误结果呈「128 行一条的错误条带」。应该先查 worksheet 的哪一行?

**参考答案**:先查 Roles 行与 Handoff 行的相位——128 行恰好是一个 warpgroup/一个 tile 的行带,症状表把它归为「同步竞争或 tile 索引不匹配」,排查对象是生产者/消费者相位、scheduler 前进(`m_idx`/`n_idx`)与哪个 warpgroup 拥有哪条行带;而不是像 NaN 那样去查描述符与 swizzle。

**练习 3**:为什么附录要求「非法访存之后重启 Python」而「挂死之后不必」?

**参考答案**:非法访存会毒化 CUDA context——后续无关调用继续失败,不重启的话你看到的所有错误都可能来自上一次崩溃而非当前代码;挂死只是内核不返回,context 通常仍完好,修完直接重跑即可(见 [appendix/debugging_warp_specialized.md:L145-L147](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L145-L147))。

---

## 5. 综合实践

**任务**:完成本讲规格规定的完整调试演练——选一个症状,为 Step 7 或 FA4 写出 roles/storage/handoff/lifetime 四行 worksheet,并给出针对该症状的逐步排查清单。

**建议选题**(避免与本讲正文已完成的「Step 7 + 死锁」重复):

- **A. FA4 + 错误结果**:症状设为「输出有限但有模式的错误值,且仅部分 Q stage 受影响」。
- **B. Step 7 + 崩溃**:症状设为「非法访存,重启 Python 后重跑仍在同一处失败」。
- **C. Step 9 + 正确但慢**:症状设为「输出正确,但相比 Step 8 几乎没有加速」。

**交付物**(四件):

1. **四行 worksheet**。按下表格式填写(以选题 A 为例的骨架,内容须自行从源码求证):

   | 行 | 内容(以 FA4 为例) |
   |---|---|
   | Roles | 从 [chapter_flash_attention/index.md:L210-L219](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L210-L219) 的角色表出发,逐角色写清 wg/warp/是否 elect |
   | Storage | S/P/O 各在 TMEM 的哪个 region、Q/K/V 在哪个 SMEM stage、acc_scale 邮箱在哪 |
   | Handoff | 从 [chapter_flash_attention/index.md:L296-L309](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L296-L309) 的完成条件表抄录并核对:屏障类型、init 数、谁到达、谁等待 |
   | Lifetime | 每个 SMEM stage / TMEM region / 邮箱槽最早何时可复用,由哪道屏障放行 |

2. **症状归类**:对照症状映射表([appendix/debugging_warp_specialized.md:L132-L143](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L132-L143))写出归类理由与「先查什么」。
3. **逐步排查清单**:按对应症状小节(死锁 [L149-L169](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L149-L169) / 崩溃 [L171-L178](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L171-L178) / 错误结果 [L180-L188](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L180-L188) / 正确但慢 [L190-L199](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L190-L199))逐条展开成「检查点 / 期望观测 / 若不符合怎么办」三列。
4. **inspect_source 搜索指令**:列出针对该症状会在生成 CUDA 中搜索的字符串及各自的判定标准。参考起点:

   ```text
   按映射表认守卫:  (warp_id_in_cta >> 2) == N   → wg_id 守卫
                    (warp_id_in_cta & 3) == N    → warp_id 守卫
                    threadIdx.x % 32) == 0       → lane_id 守卫
                    tvm_builtin_elect_one_sync_op → elect_sync
   按骨架查结构:    mbarrier_init / threadIdx.x < 1 / tcgen05 /
                    cp.async.bulk.tensor / __syncthreads();
   ```

**验证方式**:有 Blackwell GPU 时,把清单第一条做成真实故障(改一处 init 数或相位,如 4.3.4 的 `ld2mma.init(64)` 改法),确认清单能定位到它,然后改回;无 GPU 时,交付物以源码推演完成,并标注「待本地验证」的环节。

---

## 6. 本讲小结

- **先环境、再编译、再内核**:排除环境(旧 TVM 检出、非 Blackwell GPU)与编译期问题(API/dispatch/scope/生成路径)之后,异步内核的运行期故障可收敛为五种「断裂的交接」:未初始化屏障、到达数错、集体操作被守卫缩小、相位过期、存储提前复用。
- **worksheet 是调试的模型**:roles(谁发起)/storage(数据在哪)/handoff(谁到谁、信号、到达数、相位、fence 或 drain)/lifetime(何时可复用)四行写下来,再按五条核对与生成 CUDA 逐条对照。
- **检视生成代码先搜后读**:保存 `inspect_source("cuda")`,记住守卫映射(`wg_id`→右移 2、`warp_id`→按位与 3、`.init()`→`threadIdx.x < 1`),先搜 `mbarrier_init`、`tcgen05`、`cp.async.bulk.tensor`、`__syncthreads();` 等字符串,并对照 Step 7 参考骨架核对结构与四条要点。
- **按症状走分支**:死锁查屏障账本六条(到达数、init 位置、cta_sync 位置、next_tile 参与度、K-tile 数、初始相位);崩溃先重启 Python 再查 alloc 顺序/lane 守卫/dealloc 前同步/越界;错误结果先按模式(行条带/NaN/有模式有限值)分类再对号;正确但慢回到生成路径与资源(溢出、occupancy、流水线深度)。
- **一次只改一处交接**:init 数、相位、守卫、fence、store 排空、TMEM alloc/dealloc、scheduler 前进——改前改后各保存一份生成 CUDA 做 diff,测性能前先重跑正确性。

---

## 7. 下一步学习建议

本讲是单元十五(附录工具链)的最后一篇方法讲。接下来:

- **进入 u16-l1(交互式演示与图表脚本)**:换一个视角看仓库的可视化基建,`img/scripts` 下的生成脚本与 `_extra/demo` 下的交互演示能帮你直观理解 swizzle、TMEM 布局等容易调试出错的概念。
- **准备 u16-l2(综合实战 capstone)**: worksheet 将成为你设计自定义内核变体时的设计文档骨架——先写 roles/storage/handoff/lifetime,再写代码,正是本讲方法论的正向应用。
- **回读源码**:把 [appendix/debugging_warp_specialized.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md) 全文通读一遍(本讲按四个模块拆解了它,通读可补齐上下文),并对照 [chapter_gemm_advanced/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md) 的 Step 7「Checking Barrier Handoffs」一节(L305 起)做第二次跟踪练习。
