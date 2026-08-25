# u15-l6 IKET:在 DSL 内核内部标注阶段

## 1. 本讲目标

学完本讲,你应该能够:

1. 说清 IKET(In-Kernel Event Tracing)在剖析工具链中的定位:它补充了 Nsight Systems 与 Nsight Compute 都看不到的「内核内部、按 warp 角色划分」的时间线。
2. 使用 `iket.IketProfiler` 在 TIRx 内核源码中插入 `mark()`、`range_push()` / `range_pop()` 标注,并遵守「每条控制流路径上区间必须配对」的纪律。
3. 运行并读懂 `appendix/iket_example.py`,解释输出中 `producer_load`、`wait_for_data`、`consumer_compute` 三段区间的含义。
4. 把 IKET 的阶段级数据与整体内核耗时对照,并理解「插桩改变内核、带来开销」这条使用边界。

---

## 2. 前置知识

本讲是手册测量篇的第三讲,建立在以下已建立的认知之上,不再重复展开:

- **u15-l5 的三级剖析流水线**:Proton 按「哪个内核最贵」选靶 → Nsight Systems 看应用级时间线 → Nsight Compute 用硬件计数器剖析单个内核。它们回答的是「时间去哪了」。
- **三级工具的共同盲区**:nsys 看到的是一条内核从 launch 到结束的整段区间,NCU 给出的是整次 launch 的聚合指标。两者都看不到内核**内部**——哪个 warp 角色在什么时候等待、什么时候干活。
- **u13-l1 的 warp specialization**:Step 7 把一个 warpgroup 的串行控制流拆成 TMA producer、MMA consumer、writeback 三个并发角色,用 if 守卫划分。正因为多个角色共存于**同一个内核**里,「内核内部时间线」才是诊断等待与重叠的关键证据。
- **u15-l1 / u15-l2 的 TIRx 语法**:PrimFunc 的书写方式、if 守卫与协作范围(集体操作必须被其范围内全部线程一致到达)。

本讲的新名词:

| 名词 | 含义 |
|---|---|
| profiler range(区间) | 一段有开始和结束的时间范围,在时间线上显示为一根横条 |
| 瞬时事件(event) | 只有一个时间点的标记,类似打一个时间戳 |
| Perfetto | 谷歌开源的时间线可视化工具,可直接打开 `*.pftrace` 追踪文件 |
| 插桩(instrumentation) | 为了观测而在程序中插入记录代码;记录代码本身会改变被测程序 |

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [appendix/iket_example.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/iket_example.py) | 本讲主角:一个带 IKET 标注的双 warp 角色最小示例,可完整运行 |
| [appendix/benchmarking_gpu_kernels.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md) | 基准测试附录,其最后一节(L1205 起)是 IKET 的官方说明:安装、运行、标注纪律 |
| [appendix/nsys_example.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/nsys_example.py) | 对照物:host 侧用 NVTX range 标注算子,粒度停在内核之外 |
| [chapter_gemm_advanced/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md) | Step 7 的三角色内核源码,是 IKET 的真实用武之地 |

---

## 4. 核心概念与源码讲解

### 4.1 IKET range 标注:把探针写进内核源码

#### 4.1.1 概念说明

基准测试附录在工具分工表里给 IKET 留了一行,一句话说清了它的问题域:

> IKET (optional) — After adding in-kernel markers, when do marked phases run, and where do warp roles wait or overlap?
> (加入内核内标记之后,被标记的各阶段何时运行?各 warp 角色在哪里等待、在哪里重叠?)

见 [appendix/benchmarking_gpu_kernels.md:L15-L26](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L15-L26)。

IKET 的定义与动机在同一节开头([appendix/benchmarking_gpu_kernels.md:L1205-L1210](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L1205-L1210)):

- Nsight Systems 显示整个内核的开始与结束;
- NCU 把硬件指标聚合到整次 launch;
- **IKET 记录每个 warp 角色在 producer、wait、consumer 等被标记区域内何时活跃**。

关键理解:IKET 不是又一个「从外面看」的剖析器,而是**源码级插桩**。你在 TIRx 内核里写下一行 `profiler.range_push("名字")`,这行代码会随内核一起编译进设备代码,由 GPU 上的 warp 自己记录时间点。这与你此前见过的两层标注形成对照:

| 标注层 | 例子 | 粒度 | 本讲对照 |
|---|---|---|---|
| host 侧算子级 | `torch.cuda.nvtx.range("BF16 GEMM")`([appendix/nsys_example.py:L16-L19](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/nsys_example.py#L16-L19)) | 一次 Python 调用 | nsys 时间线上的大框 |
| 设备侧内核级 | nsys 自动画的 kernel 区间 | 一个内核整体 | 只有两端,中间是黑的 |
| 设备侧阶段级 | `iket` 的 `range_push`/`range_pop` | 内核内、按 warp 角色 | 本讲主题 |

#### 4.1.2 核心流程

使用 IKET 的完整回路:

```text
1. 在 PrimFunc 内创建 IketProfiler()
2. 在关键阶段前后插入 range_push("阶段名") / range_pop()
   (瞬时事件用 mark("事件名");等待区域要显式标注)
3. 把「编译 + 首次 JIT 加载 + 运行」写进一个函数,交给 iket.run()
4. iket.run 重启脚本进入 IKET 采集进程,在记录开启状态下编译并加载内核
5. postprocess 产出 JSON / *.pftrace / HTML 三类工件
6. 用 Perfetto 打开 pftrace,按 warp 泳道解读各阶段区间
```

API 有两对,纪律各不相同(依据 [appendix/benchmarking_gpu_kernels.md:L1325-L1328](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L1325-L1328)):

- `range_push()` / `range_pop()`:**栈式**配对,push 压栈、pop 弹栈,适合顺序嵌套的阶段。
- `range_start()` / `range_end()`:句柄式配对,适合区间两端分处不同控制流分支的场景。
- `mark()`:瞬时事件,只打一个时间戳,不构成区间。
- 两条硬纪律:
  1. **IKET 区间必须在 warp 可能走到的每条控制流路径上保持配对**——这与 u15-l2 讲过的「集体操作的守卫集合须等于其到达集合」是同族问题:守卫不平衡,时间线的嵌套结构就被破坏。
  2. **显式标注等待**。等待不是「什么都没发生」,而是 warp-specialized 内核里最重要的行为证据;不标等待,时间线上就会出现无法解释的空洞。

#### 4.1.3 源码精读

标注 API 从 `tvm.tirx.cuda` 导入——IKET 是 TVM 的 `tvm.tirx` 模块自带的能力,不是独立工具:

[appendix/iket_example.py:L1-L9](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/iket_example.py#L1-L9) — 模块文档串写明用途「Minimal TIRx workload with IKET ranges for two warp roles」,并从 `tvm.tirx.cuda` 导入 `iket`。

采集入口是宿主端的 `iket.run`(下文 4.2.3 精读),它要求编译过程发生在记录开启之后——这正是 `tvm.compile` 必须留在被传入函数内部的原因(见 [appendix/benchmarking_gpu_kernels.md:L1313-L1316](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L1313-L1316))。

运行前的环境准备:TVM 0.26 使用 `cutlass-4.6.0` IKET profile,把剖析依赖钉死到特定版本,需要按附录给出的精确版本安装并确认 `run-iket` 可用,见 [appendix/benchmarking_gpu_kernels.md:L1214-L1223](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L1214-L1223):

```bash
python -m pip install \
  'nvidia-cutlass-dsl[cu13]==4.6.0' \
  'nvidia-cuda-nvdisasm==13.3.73' \
  'nvidia-cuda-nvrtc==13.2.78'
run-iket --help
```

#### 4.1.4 代码实践

**实践 A:跑通官方示例(需 Blackwell GPU;无 GPU 者做源码精读替代)**

1. **实践目标**:得到第一份 IKET 追踪工件,并在 Perfetto 里亲眼看到三个阶段区间。
2. **操作步骤**:
   1. 按 u1-l3 装好 `apache-tvm==0.26.0` 与 `cuda-bindings`,再按上文安装三个钉版本的剖析依赖;
   2. 在仓库根目录执行 `python appendix/iket_example.py`(附录原文见 [appendix/benchmarking_gpu_kernels.md:L1307-L1311](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L1307-L1311));
   3. 打印出的 `IKET output directory` 即 `reports/iket-warp-roles`,把其中的 `*.pftrace` 拖入 <https://ui.perfetto.dev>。
3. **需要观察的现象**:每个 warp 一条泳道,泳道上依次出现 `producer_load`(仅 warp 0)、`wait_for_data`(两个 warp 都有)、`consumer_compute`(仅 warp 1)三色横条;`kernel_start` 是一个点事件。
4. **预期结果**:附录明确提示「warp 1 通常比 warp 0 先到达屏障,因此其 `wait_for_data` 区间更长」([appendix/benchmarking_gpu_kernels.md:L1318-L1321](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L1318-L1321))。具体毫秒数与区间比例随机器而定,**待本地验证**。
5. 无 GPU 替代:通读 `appendix/iket_example.py` 全文,在不运行的前提下画出「warp 0 / warp 1 × 时间」的预期时序草图,标注三个 range 的归属。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `iket.run` 的被传入函数里必须包含 `tvm.compile()` 与 `.jit()`,而不能在脚本顶层先编译好?

**答案**:依据 [appendix/benchmarking_gpu_kernels.md:L1313-L1316](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L1313-L1316),`iket.run` 会把当前脚本重启到 IKET 采集进程内再调用该函数;只有让编译与首次 JIT 加载发生在记录开启期间,插入的标注代码才会被捕获并正确登记。若提前编译,IKET 采集不到这次加载,追踪就不可靠。

**练习 2**:`range_push`/`range_pop` 与 `range_start`/`range_end` 各适合什么场景?如果在一个 `if warp_id == 0:` 分支里 `range_push`,却把 `range_pop` 放在分支外,会发生什么?

**答案**:`push/pop` 是栈式配对,适合顺序、可嵌套的阶段划分;`start/end` 是句柄式配对,适合区间两端位于不同控制流分支的场景(附录 [L1325-L1327](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L1325-L1327))。把 push 放分支内、pop 放分支外会破坏配对:对进入该分支的 warp,pop 弹出的可能是别人的区间;对没进入分支的 warp,pop 会下溢。这正是附录强调「区间必须在 warp 可能走到的每条控制流路径上保持平衡」的原因。

---

### 4.2 warp 角色示例:producer / wait / consumer 三段标注

#### 4.2.1 概念说明

`appendix/iket_example.py` 是一个刻意做小的 warp-specialized 内核:一个 CTA、两个 warp、256 个 float。附录对它的描述见 [appendix/benchmarking_gpu_kernels.md:L1226-L1229](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L1226-L1229):warp 0 把 256 个元素从全局内存搬进共享内存;两个 warp 在 CTA 屏障汇合;warp 1 读共享内存、计算并写出。三对 `range_push()`/`range_pop()` 分别标注 producer、wait、consumer 三个区域。

它的教学价值在于**结构同构**:「一个生产者角色 + 一个显式等待 + 一个消费者角色」正是 Step 7 中 TMA producer / MMA consumer / writeback 三角色结构的缩小版。学会在这个 30 行内核上读 IKET 时间线,就知道了该给大内核的哪些位置插探针。

#### 4.2.2 核心流程

内核的数据流与角色分工:

```text
warp 0 (producer):  inp (GMEM) --循环 8 次×32 lane--> shared (SMEM)
两个 warp:          T.cuda.cta_sync()   ← 唯一的汇合点,被 wait_for_data 区间包住
warp 1 (consumer):  shared (SMEM) --读出、×2+1--> out (GMEM)
```

每个线程搬运/计算的元素下标由两级坐标拼出:`index = i * 32 + lane`(外层第 i 轮 × warp 内 lane 号),256 个元素 = 8 轮 × 32 lane,恰好一人不重不漏。这就是 u9-l3 三要素分析中的 scope 部分:两个 `if warp_id == ...` 守卫把同一段代码分给两个角色,守卫集合互不相交且并集覆盖全部线程。

#### 4.2.3 源码精读

**探针对象与线程坐标的声明。** `IketProfiler` 在 PrimFunc **内部**创建——它是随内核一起编译的设备侧对象,而不是 host 侧的分析器:

[appendix/iket_example.py:L16-L22](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/iket_example.py#L16-L22) — 声明 `@T.prim_func` 内核;创建 `profiler = iket.IketProfiler()`;用 `T.warp_id([2])` 与 `T.lane_id([32])` 拿到 warp 号与 lane 号;分配 256 元素的 shared buffer。这四行就是使用 IKET 的全部前置声明。

**瞬时事件 `mark`。**

[appendix/iket_example.py:L24-L25](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/iket_example.py#L24-L25) — `profiler.mark("kernel_start", warp_id)` 在内核入口打一个时间戳点,并把 `warp_id` 作为参数传入,让这个事件归属到具体 warp 的泳道。它是后续所有区间的公共参考原点。

**生产者区间。**

[appendix/iket_example.py:L26-L30](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/iket_example.py#L26-L30) — 在 `if warp_id == 0:` 守卫内,`range_push("producer_load")` 与 `range_pop()` 把「8 轮循环搬 GMEM→SMEM」整段包住。注意 push 与 pop 都在同一个守卫分支内——这就是 4.1.2 说的路径平衡。

**等待区间——本示例最值得学的一行设计。**

[appendix/iket_example.py:L32-L34](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/iket_example.py#L32-L34) — `range_push("wait_for_data")` 包住 `T.cuda.cta_sync()`。这段代码**不在任何 warp 守卫内**,两个 warp 都会执行:它量的是「各自到达屏障到屏障放行之间」的等待。附录特别强调要把等待显式标出来([L1327-L1328](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L1327-L1328))——warp-specialized 内核的核心问题恰恰是「谁在等谁、等了多久」,不标等待就丢失了最重要的证据。

**消费者区间。**

[appendix/iket_example.py:L36-L41](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/iket_example.py#L36-L41) — `if warp_id == 1:` 分支内,`consumer_compute` 区间包住「读 SMEM、计算 `x*2+1`、写 GMEM」的循环,写法与生产者对称。

**宿主侧:编译、运行与正确性断言。**

[appendix/iket_example.py:L44-L56](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/iket_example.py#L44-L56) — `profile_workload()` 内先 `tvm.compile(..., tir_pipeline="tirx")` 再 `.jit()`(两者必须留在函数内的原因见练习 1),然后用 `tvm.runtime.tensor` / `tvm.runtime.empty` 准备输入输出,调用后 `sync`,最后 `np.testing.assert_array_equal(out, input*2+1)` 断言。这正是 u15-l4 确立的纪律「正确性先行」在剖析场景的体现:剖析的数据若来自一个算错的内核,毫无意义。

**宿主侧:交给 iket.run 采集。**

[appendix/iket_example.py:L59-L69](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/iket_example.py#L59-L69) — `iket.run(profile_workload, output_dir=Path("reports/iket-warp-roles"), postprocess="all", clobber=True, timeout=600.0)` 启动采集;随后遍历打印 `result.json_traces`、`result.perfetto_traces`、`result.html_reports` 三类工件路径。`postprocess="all"` 对应附录说的 JSON、`*.pftrace` 与 HTML 三种产物([appendix/benchmarking_gpu_kernels.md:L1318-L1319](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L1318-L1319))。

#### 4.2.4 代码实践

**实践 B:给两角色内核加你自己的标注(本讲主实践)**

1. **实践目标**:亲手插一次探针,并解释新增区间在时间线上的含义。
2. **操作步骤**:
   1. 把 `appendix/iket_example.py` 复制为仓库外的 `iket_my_variant.py`(**不要改动仓库源码**);
   2. 仿照原有写法做两处扩展(以下为**示例代码**,仅示意插入位置):

   ```python
   # 示例代码:在 consumer 内部再切出一个 epilogue 子区间
   if warp_id == 1:
       profiler.range_push("consumer_compute")
       for i in T.serial(ELEMS_PER_LANE, unroll=False):
           index = i * 32 + lane
           out[index] = shared[index] * T.float32(2) + T.float32(1)
       profiler.range_pop()
       profiler.mark("consumer_done", warp_id)          # 新增:消费者完工的瞬时事件
   ```

   3. 再给 warp 0 的 `producer_load` 结束后补一个 `mark("producer_done", warp_id)`;
   4. 运行 `python iket_my_variant.py`,在 pftrace 中找到两个新 mark 点。
3. **需要观察的现象**:`producer_done` 与 `consumer_done` 两个点事件的先后关系;`wait_for_data` 区间是否恰好在 `producer_done` 之后才对 warp 1 收缩。
4. **预期结果**:warp 1 的 `wait_for_data` 右端点应由 `producer_done` 的时刻决定(屏障要等最慢的 warp 0);`consumer_done` 是全内核最后一个事件。具体数值**待本地验证**(无 GPU 时,请写出这两个 mark 的预期先后顺序及理由,完成纯推演版)。
5. 校验:脚本末尾的 `assert_array_equal` 仍应通过——插桩不应改变数值结果。

#### 4.2.5 小练习与答案

**练习 1**:为什么 `wait_for_data` 区间写在两个 warp 守卫**之外**,而 `producer_load`/`consumer_compute` 写在守卫**之内**?

**答案**:`cta_sync` 是 CTA 级集体操作,必须由 CTA 内全部线程一致到达(u15-l2 的守卫纪律),所以包住它的区间要对每个 warp 都执行,才能让每个 warp 的泳道都有自己的等待条。而 producer/consumer 是角色专属工作,区间只应出现在承担该角色的 warp 的时间线上;放进守卫内恰好同时满足了「区间按路径平衡」与「区间归属正确」两个要求。

**练习 2**:这个示例与 Step 7 的三角色结构如何对应?若把 IKET 用到 Step 7,至少应插入哪几个 range?

**答案**:warp 0 ≈ TMA producer,warp 1 ≈ 消费者,`cta_sync` ≈ full/empty 屏障交接(Step 7 中细化为 `tma2mma`/`mma2tma` 等四道屏障)。对照 [chapter_gemm_advanced/index.md:L202-L229](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L202-L229)(WG1 warp 3 的 TMA producer 循环:先 `mma2tma.wait` 等 SMEM 空闲、再 `tma_load`、再 arrive)与 [chapter_gemm_advanced/index.md:L231-L239](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L231-L239)(WG1 warp 0 的 MMA consumer),至少应为每个角色各标两段:`tma_wait_empty` + `tma_load`(producer),`mma_wait_full` + `mma_issue`(consumer),外加 writeback 的 `ld_writeback` 与 `tma_store`,把「等」与「干」分开——这正是 Step 7 时间线图想表达的 overlap 证据。

---

### 4.3 阶段耗时解读:pftrace、工件与整体耗时的对照

#### 4.3.1 概念说明

拿到追踪文件后,解读围绕三个问题:

1. **每个阶段多久?** 区间横条的长度即该 warp 在该阶段消耗的时间。
2. **谁在等、等多久?** 等待区间的长度直接量化角色间的失衡。本例中 warp 1 先到屏障,`wait_for_data` 更长;若在 Step 7 中看到 consumer 的 `wait_full` 占比极高,说明 producer 供数不足。
3. **阶段拼起来与整核什么关系?** 对单个 warp,各区间与未标注的空隙首尾相接,总跨度等于该 warp 视角的内核时长:

\[ T_{\text{warp}} \;=\; \sum_{r \,\in\, \text{ranges}(w)} \ell(r) \;+\; \sum_{g \,\in\, \text{gaps}(w)} \ell(g) \]

多 warp 并行时,内核总时长由最晚收工的 warp 决定:\(T_{\text{kernel}} = \max_w T_{\text{warp}}\)。因此**各角色区间长度之和可以超过内核总时长**(角色并行),这不再是 u15-l4 里「多个内核重叠导致时长之和超标」的 host 侧现象,而是同一内核内部角色并行的直接体现——重叠本身就是 warp specialization 的目的。

#### 4.3.2 核心流程

解读一份 IKET 追踪的推荐顺序:

```text
1. 找 kernel_start 点事件 → 各 warp 泳道的起点对齐情况(启动偏斜)
2. 逐 warp 看区间序列 → 该角色的「工作/等待」交替节奏
3. 对齐不同 warp 的泳道 → 找等待区间的右端点由谁决定(谁是最慢角色)
4. 量化:某等待区间占该 warp 总跨度比例 = 失衡度
5. 对照未插桩基线 → 确认结论没有插桩开销的污染
```

#### 4.3.3 源码精读

**三类工件。** [appendix/iket_example.py:L67-L69](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/iket_example.py#L67-L69) 遍历打印 `json_traces`、`perfetto_traces`、`html_reports` 三类产物:JSON 适合程序化统计区间长度;`*.pftrace` 在 Perfetto 里可视化;HTML 报告开箱即看。附录建议「把 pftrace 装入 Perfetto 检查 producer_load、wait_for_data、consumer_compute 三段」([appendix/benchmarking_gpu_kernels.md:L1318-L1321](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L1318-L1321))。

**最重要的使用边界——插桩开销。** [appendix/benchmarking_gpu_kernels.md:L1330-L1334](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L1330-L1334) 给出明确告诫:IKET 插入的记录代码会**改变生成的内核并增加开销**。所以:

> 用 IKET 追踪研究阶段与 warp 角色;要报告延迟,请用未插桩的 CUDA events 基准。

这与 u15-l5 末尾「NCU 的 Duration 只属被剖析运行,跨实现比较须回未剖析基线」是同一条纪律在两件工具上的体现——凡重放/插桩类观测,数字只用于诊断,不用于报告。

**支持的架构与校验。** 同段还说明 IKET 支持 Hopper 及更新架构,并会按钉死的 profile 校验 CUTLASS DSL 包、NVRTC、`nvdisasm` 等二进制版本——所以 4.1.3 的三行 `pip install` 不是可选项。H100 用户只需把脚本里的 `sm_100a` 改成 `sm_90a`([appendix/benchmarking_gpu_kernels.md:L1320-L1321](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L1320-L1321))。

**延伸 API。** 完整 API 与追踪选项在两处上游文档:[TVM 的 iket.py](https://github.com/apache/tvm/blob/v0.26.0/python/tvm/backend/cuda/iket.py) 与 [NVIDIA IKET guide](https://github.com/NVIDIA/cutlass/blob/v4.6.0/media/docs/pythonDSL/cute_dsl_general/iket_profiling.rst),链接见 [appendix/benchmarking_gpu_kernels.md:L1335-L1338](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L1335-L1338)。

#### 4.3.4 代码实践

**实践 C:阶段耗时 vs 整体内核耗时(需 GPU;无 GPU 者完成推演版)**

1. **实践目标**:建立「IKET 阶段数据只做诊断、报告延迟用 CUDA events」的操作习惯,亲手量一次两者的差距。
2. **操作步骤**:
   1. 依 u15-l4 的计时规范写一个未插桩基线:把 `appendix/iket_example.py` 的内核复制一份、**删掉全部 `profiler.*` 行**,用 CUDA events 预热后多次计时取中位数;
   2. 再对带插桩版本用同样边界计时;
   3. 在 pftrace 里量出 warp 1 的 `wait_for_data` 长度与总跨度。
3. **需要观察的现象**:插桩版延迟 ≥ 未插桩版;`wait_for_data` 在 warp 1 泳道上明显长于 warp 0。
4. **预期结果**:两个延迟的差值即插桩开销的量级;若你的标注密度更高(如综合实践中给 Step 7 每角色标 6 段),开销比例还会放大。具体数字**待本地验证**。
5. 无 GPU 替代(源码阅读型实践):写出「若删掉 `wait_for_data` 标注,pftrace 上会丢失哪条证据」的文字分析——答案应指向「warp 1 先到屏障」这一等待证据将变成无法解释的空白段。

#### 4.3.5 小练习与答案

**练习 1**:nsys 时间线上该内核只显示一条 3.2 µs(假设)的横条,而 IKET 显示 warp 0 与 warp 1 各有一套区间,且两套区间的总长都约等于 3.2 µs。这说明了什么?

**答案**:nsys 的横条是内核整体的 launch 区间,粒度到此为止;IKET 把这 3.2 µs 按角色切开,表明两个 warp 在同一时间窗内**并行**地各自走完「工作+等待」。这也演示了 4.3.1 的公式:每 warp 的区间加空隙等于其总跨度,而两 warp 的总跨度又都受 \(T_{\text{kernel}}=\max_w T_{\text{warp}}\) 约束。

**练习 2**:你在 IKET 追踪里发现 consumer 的 `wait_full` 区间占了其总跨度的 90%,下一步该查什么?

**答案**:等待 full 屏障说明上游供数不足。按 u13-l1 的四道屏障结构,应依次检查:producer 的 `tma_load` 区间是否过长(SMEM 带宽或 TMA 排队)、`mma2tma`(empty 屏障)是否迟迟不放行缓冲(stage 深度不足)、以及 producer 是否因 `elect_sync` 单线程发起而发射速率受限。这正好接上 u15-l7 的调试方法论:先定位「谁在等谁」,再进入对应症状分支。

**练习 3**:为什么本例用 `np.testing.assert_array_equal`(精确相等)而不是 u9-l2 GEMM 验证用的 `assert_close(rtol=2e-2, atol=1e-2)`?

**答案**:本例是纯逐元素的 `x*2+1`,fp32 单次乘加不引入舍入误差累积,参考值与内核输出应逐位一致,可用精确相等;GEMM 的 fp16 输入、fp32 累加在归约维度上做大量浮点加法,舍入顺序与参考实现不同,必须给容差。容差选择应与算子的数值性质匹配,而不是一律宽松或一律严格。

---

## 5. 综合实践

**任务:给 Step 7(或 FA4)设计一份完整的 IKET 标注方案,并(有条件时)实施与解读。**

1. **设计阶段(人人可做)**:以 [chapter_gemm_advanced/index.md:L202-L239](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L202-L239) 的 producer/consumer 角色代码为对象,产出一张「角色 × 区间」设计表,列名建议:`range 名 / 所在守卫 / 包住的代码 / 预期回答的问题`。最低要求覆盖:
   - producer:`tma_wait_empty`(包住 `mma2tma.wait`)与 `tma_load`(包住 `tma_load` 调用);
   - MMA consumer:`mma_wait_full` 与 `mma_issue`;
   - writeback:`ld_tmem`、`write_dsmem`、`tma_store`;
   - 每个 `while tile_scheduler.valid()` 迭代入口一个 `mark("tile_begin")`。
2. **平衡性自查**:逐行核对每个 push 都有一条同守卫、同循环层级的 pop;特别检查 `@T.inline` 展开后的路径(与 u15-l1 的 inline 语义呼应)。
3. **实施阶段(需 Blackwell/Hopper GPU)**:复制 Step 7 内核源码出仓库、插入标注、按 4.1.3 安装依赖、用 `iket.run` 包住编译加载运行、在 Perfetto 中量出各区间的中位长度。
4. **解读阶段**:回答两个问题——(a) 稳态下 producer 的 load 与 consumer 的 issue 重叠率是多少?(b) 哪个等待区间最长,对应 u13-l1 四道屏障中的哪一道?把结论与 Step 7 的 warp-specialization 时间线图互相印证。
5. **对照阶段**:用 u15-l4 的 CUDA events 规范测未插桩基线,报告插桩开销比例,并写明「本追踪仅用于诊断,延迟以基线为准」。

---

## 6. 本讲小结

- IKET(In-Kernel Event Tracing)是**源码级插桩**:在 TIRx PrimFunc 内创建 `iket.IketProfiler()`,用 `mark()` 打瞬时事件、`range_push()`/`range_pop()`(或 `range_start()`/`range_end()`)圈阶段,随内核一起编译进设备代码。
- 它补齐了工具链的最后一块:nsys 只见内核两端、NCU 只给整次 launch 的聚合指标,IKET 给出**内核内部按 warp 角色划分**的时间线,直接回答「谁在等、谁在干、重叠多少」。
- 两条硬纪律:区间必须在 warp 的**每条控制流路径上配对平衡**;**等待区域必须显式标注**(示例中 `wait_for_data` 包住 `cta_sync` 是最值得模仿的设计)。
- 使用回路:`iket.run(workload_fn)` 会重启脚本进采集进程,`tvm.compile` 与 `.jit()` 必须留在被传入函数内;依赖按 `cutlass-4.6.0` profile 钉版本安装;产物为 JSON、`*.pftrace`(用 Perfetto 打开)与 HTML 三类。
- 插桩会改变生成的内核并带来开销:IKET 数字只用于研究阶段与角色,**报告延迟必须回到未插桩的 CUDA events 基线**。
- 各角色区间长度之和可超过内核总时长——这不是异常,而是 warp specialization 角色并行的直接证据;内核总时长由最晚收工的 warp 决定。

---

## 7. 下一步学习建议

- **下一讲 u15-l7「调试 warp-specialized 内核」**:本讲的「等待区间定位谁在等谁」正是该讲 roles/storage/handoff/lifetime 工作表与症状分类法的前置证据;两讲合起来构成「先量、再诊」的完整闭环。
- 想看 IKET 在真实性能分析里的位置,可回读 u13-l4 的九步性能表,思考每一步优化的「增益归因」若要有内部证据,分别需要标哪些 range。
- 上游文档:[TVM `python/tvm/backend/cuda/iket.py`](https://github.com/apache/tvm/blob/v0.26.0/python/tvm/backend/cuda/iket.py) 看完整 API 与采集选项;[NVIDIA IKET guide](https://github.com/NVIDIA/cutlass/blob/v4.6.0/media/docs/pythonDSL/cute_dsl_general/iket_profiling.rst) 看追踪格式与 Perfetto 用法细节。
- 手册最后一单元(u16)将把包括 IKET 在内的全部工具用于 capstone 实战:为你的内核变体建立「设计→实现→验证→评测→诊断」的完整证据链。
