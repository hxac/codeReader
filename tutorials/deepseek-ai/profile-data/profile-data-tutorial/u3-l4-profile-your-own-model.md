# 自建 profiling 与生态项目连接

> 所属单元:u3 · 定量分析与二次实践(advanced)
> 前置讲义:u3-l1(用 Python 做轨迹定量分析)。本讲是该系列的收官篇:前面十三讲都在「读」仓库里的三份轨迹,本讲教你「造」一份同格式的轨迹,并把轨迹里看到的内核家族连接回它们的开源实现。

## 1. 本讲目标

学完本讲,你应该能够:

1. 使用 `torch.profiler.profile` 的 `activities` 与 `record_shapes` 参数,采集一个小模型的 CPU + CUDA 活动,并用 `record_function` 打出自定义注解。
2. 用 `prof.export_chrome_trace("my_trace.json")` 导出与仓库三份轨迹**同格式**的 Chrome Trace JSON,并用 chrome://tracing 打开查看。
3. 编写对照脚本,逐字段比较自制轨迹与 `train.json` 的 `schemaVersion` / `distributedInfo` / `traceEvents` / `traceName` 结构,并解释差异从何而来。
4. 用 u3-l1 的 `analyze_trace.py` 分析自制轨迹、输出 Top-10 内核,与仓库轨迹的内核族谱对照。
5. 把轨迹证据(注解体系、`dpsk::` 内核命名空间、流与时长形态)映射回 DualPipe、DeepEP、DeepGEMM 三个开源仓库,理解这些轨迹背后的工程决策。

本讲的立场转换是关键:前几讲你是「轨迹的读者」,本讲结束后你是「轨迹的生产者」——能生产,才算真正读懂了格式。

## 2. 前置知识

本讲需要的分布式与 CUDA 概念都已在前面各讲建立,这里做一次集中回顾,并补两个新工具:

- **`torch.profiler` 是什么**:PyTorch 内置的性能采集器。它底层经由 kineto 调用 CUPTI(CUDA 官方的采集接口)——u2-l1 讲过的 `correlation` 外键正是 CUPTI 分配的,所以你在自己轨迹里也会看到一模一样的关联机制。仓库 README 明说三份数据「captured using the PyTorch Profiler」,即本仓库三份 JSON 全部是 `export_chrome_trace` 的产物。
- **上下文管理器(context manager)**:Python 的 `with` 语法。`with profile(...) as prof:` 表示进入时开始采集、退出时停止并聚合——导出动作必须写在 `with` 块**之后**(聚合完成才有数据可导)。
- **`record_function` 打点**:`with record_function("名字"):` 包住一段代码,轨迹里就会出现一条同名 `user_annotation` 事件。u2-l2 精读的 `1F1B`、`attn(F)` 等 42 条注解,就是训练框架里成百上千个 `record_function` 打点中被采集到的那些。
- **`schedule` 与 warmup**:真实训练的第一步常伴随编译、内存池扩张等一次性开销,直接采集会污染统计。`schedule(wait=..., warmup=..., active=...)` 让采集器先跳过若干步、再预热若干步、最后只记录 active 窗口;每步末尾调用 `prof.step()` 推进状态机,轨迹里随之出现 `ProfilerStep#N` 注解。
- **CUDA Stream 回顾**(u3-l2):stream 是 GPU 上的任务队列,不同流的内核可以并行。本讲综合实践会用 `torch.cuda.Stream()` 造一条侧流,亲手复现「通信-计算重叠」的时间线形态。
- **运行环境**:需要一个装了 PyTorch 的 Python 环境;要采到 CUDA 活动(内核、`cuda_runtime`)必须有 NVIDIA GPU——本机没有的话,Colab(运行时类型改为 GPU)是最便捷的替代。若两者都没有,本讲所有脚本都能退化为 `activities=[ProfilerActivity.CPU]` 只采 CPU 侧,结论方法不变、只是看不到 `kernel` 事件。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关键锚点 |
| --- | --- | --- |
| `README.md` | 唯一的文档源码:声明采集工具、场景配置与生态链接 | [L3](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L3)、[L11-L12](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L11-L12)、[L22](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L22)、[L30](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L30) |
| `train.json` | 结构对照的主样本(EP64 训练轨迹,14240 条事件) | [L3-L4](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L3-L4)、[L70-L71](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L70-L71)、[L14416-L14436](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L14416-L14436)、[L31324-L31338](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31324-L31338)、[L35712](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L35712)、[L31524](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31524)、[L97653](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97653) |
| `prefill.json` | DeepGEMM 内核的第二个证据来源 | [L176454](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L176454) |
| `decode.json` | 低延迟内核证据(单行压缩,只能整体引用) | [L1](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1) |
| `analyze_trace.py` | u3-l1 第 5 节产出的通用分析脚本(示例代码,教程实践中创建) | 本讲直接复用,不修改 |

> 提醒:本讲会引用大量 PyTorch API。仓库本身**不含任何 Python 代码**,因此所有脚本均为「示例代码」——它们不在仓库中,需要你自行创建(建议放在仓库外的练习目录)。仓库能提供的「真实源码」是三份轨迹 JSON 与 README,本讲的源码精读环节全部锚定在这四份文件上。

## 4. 核心概念与源码讲解

### 4.1 torch.profiler 基本用法:从仓库轨迹反推采集代码

#### 4.1.1 概念说明

`torch.profiler.profile` 是一个采集器上下文。四个最常用参数:

| 参数 | 作用 | 对应轨迹中的现象 |
| --- | --- | --- |
| `activities` | 采集哪些侧的活动:`ProfilerActivity.CPU`(算子、Python 打点)、`ProfilerActivity.CUDA`(内核、显存拷贝) | 只选 CPU 时没有 `kernel`/`cuda_runtime` 事件 |
| `record_shapes` | 记录算子的输入张量形状 | `cpu_op` 事件的 `args` 里多出 `Input Dims` 字段 |
| `schedule` | 按 wait/warmup/active 分窗口采集 | 出现 `ProfilerStep#N` 注解 |
| (上下文)`record_function("名字")` | 用户自定义打点 | 出现 `cat == "user_annotation"` 的同名事件 |

反向推理是本讲的核心方法:**轨迹里出现什么,就说明采集时开了什么**。这不是猜测——每一条都能在 `train.json` 里找到实证。

#### 4.1.2 核心流程

```
import torch; from torch.profiler import profile, ProfilerActivity, record_function

with profile(activities=[CPU, CUDA],          # 两侧都采
             record_shapes=True,               # 记录输入形状
             schedule=schedule(wait=1, warmup=1, active=2)) as prof:
    for step, (x, y) in enumerate(loader):
        with record_function("我的注解"):      # → user_annotation 事件
            out = model(x); loss = crit(out, y)
        loss.backward(); optimizer.step(); optimizer.zero_grad()
        prof.step()                            # → ProfilerStep#N 注解

prof.export_chrome_trace("my_trace.json")      # 必须在 with 块之后
```

#### 4.1.3 源码精读:train.json 里的三件「采集指纹」

**(1) 采集工具的官方声明。** [README.md:L3](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L3) 明确写道「The profiling data was captured using the PyTorch Profiler」,并给出 chrome://tracing 的查看方式。这一行同时是本讲合法性的依据:你要复刻的不是「某种类似格式」,而正是同一工具的同一导出接口。

**(2) `ProfilerStep#1`——schedule 状态机的指纹。** 看 [train.json:L14416-L14422](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L14416-L14422):

```json
{
  "ph": "X", "cat": "user_annotation", "name": "ProfilerStep#1", "pid": 1166, "tid": 1166,
  "ts": 1740461679470813, "dur": 112202,
  "args": { "External id": 4609,"Ev Idx": 2040 }
}
```

这条事件 `dur=112202µs≈112.2ms`,套住了整个采集窗口。它说明采集代码用了 `schedule` + `prof.step()`(不用 schedule 就不会有 `ProfilerStep#N`);编号只到 `#1`,说明 active 窗口只保留了一个 step(schedule 具体参数 README 未给出,待确认)。你自制轨迹时若不用 schedule,导出的文件里就**没有**这类事件——这是最快的自检点。

**(3) `1F1B`——record_function 打点的指纹。** 紧随其后,[train.json:L14423-L14429](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L14423-L14429) 与 [train.json:L14430-L14436](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L14430-L14436):

```json
{ "ph": "X", "cat": "user_annotation", "name": "1F1B", ... "dur": 112168, ... }
{ "ph": "X", "cat": "user_annotation", "name": "combine(B)", ... "dur": 626, ... }
```

`1F1B` 与 `combine(B)` 都是框架代码里 `record_function("1F1B")` 这类打点留下的 `user_annotation`(u2-l2 已数出全文件 42 条、10 类 stage 注解)。注意它们同样携带 `External id`——u2-l1 的区间嵌套反查法对自制轨迹的注解一样适用。

**(4) `Input Dims` 缺席——`record_shapes` 未开启的实证。** 作者对 `train.json` 全文检索 `Input Dims`,结果为 **0 次**。`cpu_op` 事件(如 [train.json:L73-L78](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L73-L78) 的首条事件)的 `args` 只有 `External id`/`Ev Idx` 等字段。这说明仓库轨迹采集时**没有**开 `record_shapes`。你在自己的轨迹里开启它,`args` 就会多出 `Input Dims`——「参数决定字段」的活例子。

#### 4.1.4 代码实践

**实践一:最小采集脚本,亲眼看到 user_annotation**

1. **实践目标**:跑通一个 10 行的采集脚本,验证 `record_function` 的名字确实出现在轨迹里。
2. **操作步骤**(示例代码):

   ```python
   #!/usr/bin/env python3
   # 示例代码:mini_profile.py —— 最小 torch.profiler 采集
   import torch
   from torch.profiler import profile, ProfilerActivity, record_function

   dev = "cuda" if torch.cuda.is_available() else "cpu"
   acts = [ProfilerActivity.CPU, ProfilerActivity.CUDA] if dev == "cuda" \
          else [ProfilerActivity.CPU]
   model = torch.nn.Sequential(torch.nn.Linear(64, 256), torch.nn.Linear(256, 64)).to(dev)
   x = torch.randn(128, 64, device=dev)

   with profile(activities=acts, record_shapes=True) as prof:
       for _ in range(3):
           with record_function("my_forward"):        # 自定义打点
               y = model(x)
               loss = y.pow(2).mean()
           loss.backward()
       if dev == "cuda":
           torch.cuda.synchronize()                   # 确保 GPU 活动落入窗口

   prof.export_chrome_trace("my_trace.json")          # 在 with 之后导出
   print("已导出 my_trace.json;注解应为 3 条 my_forward + 反向打点")
   ```

3. **需要观察的现象**:目录下生成 `my_trace.json`;用 chrome://tracing 打开(u1-l2 的方法),CPU 轨道上能看到 3 段 `my_forward` 色块;点选任一 `aten::linear` 算子,详情面板的 args 中出现 `Input Dims`。
4. **预期结果**:脚本自身打印的计数与时间线一致;若 `dev == "cpu"`,时间线只有 CPU 轨道、没有 `kernel` 事件(这不是错误,是 `activities` 的作用)。Colab T4 上的具体外观待本地验证。
5. 若导出文件为空,九成是把 `export_chrome_trace` 写进了 `with` 块内——聚合尚未完成。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `train.json` 里没有 `Input Dims`,而你的轨迹里有?
**答案**:该字段只有 `record_shapes=True` 时才记录。作者实测 `train.json` 全文 `Input Dims` 出现 0 次,说明仓库采集时未开启此参数;实践一脚本显式开启,故字段出现。

**练习 2**:`ProfilerStep#1` 的 `dur=112202` 与 `1F1B` 的 `dur=112168` 只差 34µs,说明了什么?
**答案**:两者几乎同区间——`ProfilerStep#1` 是 schedule 状态机的外壳,`1F1B` 是框架在 step 内打的点,前者套住后者。也再次提醒 u2-l2 的结论:注解时长是 CPU 墙钟,不是 GPU 执行时长。

**练习 3**:不用 schedule 时轨迹里会缺什么?什么时候必须用?
**答案**:缺 `ProfilerStep#N` 注解。当你关心「每个训练 step 的耗时分布」或想跳过预热期的一次性开销(编译、显存池扩张)时必须用;单次 forward 的简单采集则不必。

### 4.2 export_chrome_trace 导出与结构对照

#### 4.2.1 概念说明

`prof.export_chrome_trace(path)` 把聚合结果写成 Chrome Trace Event 格式的 JSON——与仓库三份文件**同一格式**。u1-l3 已经正向解剖过它的顶层四字段;本讲反向验证:自己导出一份,逐字段对表。

| 顶层字段 | train.json 的值(实测) | 自制单卡轨迹的预期 | 异同说明 |
| --- | --- | --- | --- |
| `schemaVersion` | 1(见 [train.json:L3](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L3)) | 1 | 同一导出器,同版本规范 |
| `deviceProperties` | 8 块 NVIDIA H800,132 SM、显存约 79.1 GiB([train.json:L4](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L4) 起) | 本机 GPU × 1(Colab T4 则为 Tesla T4) | 卡数=可见设备数;卡型不同,支持的内核族也不同(见 4.3) |
| `distributedInfo` | `{"backend": "nccl", "rank": 0, "world_size": 64}`([train.json:L70](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L70)) | 未初始化分布式时可能缺失或为 `world_size: 1` | 待本地验证;这是「分布式采集」与「单卡采集」最直接的判据 |
| `traceEvents` | 14240 条([train.json:L71](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L71) 起) | 数百~数千条 | `cat` 派生链相同:`cpu_op`→`cuda_runtime`→`kernel`,外加 `ac2g` 箭头与 `user_annotation` |
| `traceName` | `traces/prof_ep64_tp1_varsm_hidden7168_seqlen4096.json`([train.json:L97653](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97653)) | 通常无或有默认名 | 仓库作者用它编码实验配置(EP64/TP1/hidden7168/seqlen4096),堪称「文件名即元数据」 |

另一个值得注意的差异是**排版**:train/prefill 是格式化多行,decode 是单行压缩(u1-l3、u3-l1)。同为 PyTorch 导出却排版不同,可能是版本差异或导出后压缩所致(待确认)——这正是 u3-l1 强调「分析脚本不得假设排版」的现实理由。

#### 4.2.2 核心流程

```
with profile(...) as prof: 运行模型(with 块退出 → 停止采集、聚合)
    ↓
prof.export_chrome_trace("my_trace.json")   ← 写出 Chrome Trace JSON
    ↓
两条消费路径:
    ├─ chrome://tracing / ui.perfetto.dev    → 人眼浏览(u1-l2)
    └─ analyze_trace.py my_trace.json 10     → 定量报告(u3-l1)
    ↓
与 train.json 逐字段对照 → 差异清单 = 「分布式训练 vs 单卡小模型」的最小摘要
```

#### 4.2.3 源码精读:对照的基准点

**(1) 身份三行。** [train.json:L3-L4](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L3-L4) 与 [train.json:L70-L71](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L70-L71):

```json
"schemaVersion": 1,
"deviceProperties": [
...
"distributedInfo": {"backend": "nccl", "rank": 0, "world_size": 64},
"traceEvents": [
```

你的对照脚本只需抓这四行对应的字段,就能给自制轨迹「验明正身」。

**(2) traceName 的信息量。** [train.json:L97653](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97653) 的 `traces/prof_ep64_tp1_varsm_hidden7168_seqlen4096.json` 把 EP/TP/隐藏维/序列长全部编码在文件名里。它还将在 4.3 节与 DeepGEMM 内核的模板参数互相印证。

**(3) 内核事件的形态基准。** 对照内核详情时以 [train.json:L31324-L31338](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31324-L31338) 的 DeepEP dispatch 内核为样本:`args` 里的 `stream`/`correlation`/`grid`/`block`/`registers per thread` 等字段,你的自制轨迹里的每个内核同样会有(Colab 上数值不同)。换句话说,**u2/u3 全部分析方法对流没有任何偏向**,拿来即用。

#### 4.2.4 代码实践

**实践二:结构对照脚本(即本讲规格中的主实践任务)**

1. **实践目标**:录制一个小模型的 forward+backward 并导出;用脚本把 `my_trace.json` 与 `train.json` 的顶层结构并排打印。
2. **操作步骤**(示例代码):

   ```python
   #!/usr/bin/env python3
   # 示例代码:compare_structure.py —— 自制轨迹 vs 仓库轨迹的顶层结构对照
   import json

   def brief(path):
       with open(path, encoding="utf-8") as f:
           d = json.load(f)
       ev = d.get("traceEvents", [])
       n_dims = sum(1 for e in ev
                    if e.get("cat") == "cpu_op"
                    and "Input Dims" in e.get("args", {}))
       dp = d.get("deviceProperties", [])
       return {
           "schemaVersion": d.get("schemaVersion"),
           "distributedInfo": d.get("distributedInfo", "<缺失>"),
           "GPU 卡型与数量": f'{len(dp)} x {dp[0]["name"] if dp else "?"}',
           "traceEvents": len(ev),
           "cat=cpu_op 且含 Input Dims": n_dims,
           "traceName": d.get("traceName", "<缺失>"),
       }

   for p in ["my_trace.json", "train.json"]:
       print(f"== {p} ==")
       for k, v in brief(p).items():
           print(f"  {k}: {v}")
   ```

   运行前先完成实践一(生成 `my_trace.json`),然后:

   ```bash
   python3 compare_structure.py
   python3 analyze_trace.py my_trace.json 10    # u3-l1 的脚本,输出 Top-10 内核
   ```

3. **需要观察的现象**:`my_trace.json` 一侧 `distributedInfo` 缺失或 `world_size=1`;`train.json` 一侧是 64;`Input Dims` 计数一侧非零、一侧为 0;Top-10 榜里全是 `aten::`/`void ...elementwise...` 之类的通用内核,**没有任何 `dpsk::` 前缀**。
4. **预期结果**:两侧 `schemaVersion` 相同;自制轨迹的 `kernel` 数远小于 963(train 的实测值);CPU-only 环境下 `analyze_trace.py` 的 kernel 榜为空——这本身就是「activities 只选了 CPU」的指纹。Colab T4 与本机 GPU 的具体输出待本地验证。
5. 常见坑:`analyze_trace.py` 报 `distributedInfo` 相关错误时,检查你是否在单卡环境运行了期待分布式的代码——脚本里已用 `.get()` 兜底(u3-l1 的 `load_trace` 写法)。

#### 4.2.5 小练习与答案

**练习 1**:自制轨迹的 Top-10 里为什么不可能出现 `dpsk::ep::internode::dispatch`?
**答案**:该内核来自 DeepSeek 的 DeepEP 库,只有在安装并调用了 DeepEP 的集群上才会出现;普通 PyTorch 环境的算子是 `aten::`/CUTLASS/TF32 通用内核。内核名前缀是「用了谁家的库」的可靠指纹。

**练习 2**:`export_chrome_trace` 与 `prof.key_averages().table(...)`(可在终端直接打印平均耗时表)各适合什么场景?
**答案**:前者产出完整 JSON,适合本教程的逐事件分析与可视化;后者是聚合好的文本表,适合快速看「哪个算子最耗时」。两者数据同源。注意较新版本 PyTorch 中排序键名可能有变化(如 `self_cuda_time_total` 与 `self_device_time_total`),以本机文档为准(待本地验证)。

**练习 3**:为什么 Colab T4 的轨迹里找不到 FP8 GEMM 内核?
**答案**:T4 是图灵架构(计算能力 7.5),而仓库轨迹中的 `fp8_gemm_kernel` 依赖 Hopper(计算能力 9.0)的 TMA 等硬件特性——证据就在内核签名里(见 4.3.3)。

### 4.3 DualPipe / DeepEP / DeepGEMM:从轨迹证据到开源实现

#### 4.3.1 概念说明

仓库轨迹里反复出现的 `dpsk::` 是 DeepSeek 自研 CUDA 内核的命名空间(dpsk = DeepSeek 的缩写式前缀)。把轨迹证据映射回开源仓库,依据有三层:**README 的显式链接**、**内核命名空间**、**行为特征**(时长量级、所在流、次数结构)。汇总成一张映射表:

| 轨迹证据(本仓库实测) | 开源仓库 | README 依据 | 对应的工程决策 |
| --- | --- | --- | --- |
| `attn/mlp×F/B/W` + `dispatch/combine×F/B` 共 10 类注解,每种恰 4 次;每轮以 `combine(B)` 打头 | [deepseek-ai/dualpipe](https://github.com/deepseek-ai/dualpipe) | [README.md:L11-L12](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L11-L12) | DualPipe「先反后前」成对编排,用计算填充流水线气泡 |
| `dpsk::ep::internode::dispatch/combine`(train/prefill,毫秒级,独立通信流);`dispatch_ll/combine_ll`(decode,微秒级,与计算同流) | [deepseek-ai/DeepEP](https://github.com/deepseek-ai/DeepEP) | [README.md:L30](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L30) | MoE all-to-all 的 normal 与 low-latency 两代实现;后者 RDMA 发出即释放 SM |
| `dpsk::gemm::fp8_gemm_kernel`(prefill)、`cutlass::device_kernel<dpsk::grouped_gemm::...>`(train) | [deepseek-ai/DeepGEMM](https://github.com/deepseek-ai/DeepGEMM) | README 未直接给出链接;依据 `dpsk::gemm` 命名空间与该仓库的 FP8/grouped GEMM 功能(合理推断,待确认) | Hopper 上的 FP8 稠密与分组 GEMM,服务 MoE 专家计算 |

方法论的要点:**轨迹是测量,仓库是实现**。README 只显式链接了 dualpipe 与 DeepEP 两个仓库;DeepGEMM 的映射靠命名空间推断——这也是「不编造」原则的示范:推断要标注为推断。

#### 4.3.2 核心流程

```
看到一条轨迹证据(注解名 / 内核名 / 流与时长形态)
    ↓ 问三个问题
它叫什么?(命名空间、模板参数)
它在哪里跑?(stream、pid、时长量级)
它出现多少次?(次数结构与层数/微批数的整除关系,u2-l4/u2-l6 的方法)
    ↓
映射到开源仓库的对应模块(调度算法 / 通信库 / 数学库)
    ↓
用 README 的文字描述交叉验证(如 L30 的「RDMA 发出后释放全部 SM」)
```

#### 4.3.3 源码精读:三条映射的证据链

**(1) DualPipe:注解体系就是调度算法的投影。** [README.md:L11-L12](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L11-L12) 写明:训练轨迹展示的是「a pair of individual forward and backward chunks in DualPipe」,每 chunk 含 4 个 MoE 层,配置 EP64/TP1/4K 序列长,且 PP 通信未计入。u2-l2 的实测与之严丝合缝:10 类 stage 注解**每种恰出现 4 次**(= 4 个 MoE 层),每轮以 [train.json:L14430-L14436](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L14430-L14436) 的 `combine(B)` 打头、反向与前向交错——这正是 DualPipe 论文描述的「先反后前」成对分块。去 [dualpipe 仓库](https://github.com/deepseek-ai/dualpipe) 能读到该调度的实现;读完后回看这 42 条注解,它们不再是色块,而是算法每一步的落点。

**(2) DeepEP:normal 与 low-latency 两代内核的形态对比。** [README.md:L30](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L30) 对解码 all-to-all 的描述是:「after RDMA messages are issued, all GPU SMs are freed」——并附上 [DeepEP](https://github.com/deepseek-ai/DeepEP) 链接。轨迹证据分两代:

- normal 代(train/prefill):[train.json:L31324-L31338](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31324-L31338) 的 `dpsk::ep::internode::dispatch`,`dur=3320µs`(毫秒级),`args.stream=27`(独立通信流);[train.json:L35712](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L35712) 的 combine 同样在 stream 27。
- low-latency 代(decode):内联于 [decode.json:L1](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1) 的 `dispatch_ll/combine_ll`,各 235 次、17~19µs 发送段与 58~86µs 等待段(u2-l5 实测),与计算**同在 stream 7**。

同一功能、两代实现、两种时间线形态——README 那句「RDMA 发出后释放全部 SM」在 u2-l5/u3-l2 里被逐微秒验证。这就是「轨迹 ↔ 仓库」互相照亮的典型。

**(3) DeepGEMM:模板参数与 traceName 的互证。** train 的 MoE 专家分组 GEMM 是 [train.json:L31524](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31524) 的 `cutlass::device_kernel<dpsk::grouped_gemm::cuda::fp8_ptp128c::GemmKernel>`(在计算流 `tid=7`,u3-l1 实测时长 2.0~2.2ms);prefill 的稠密 FP8 GEMM 是 [prefill.json:L176454](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L176454):

```
void dpsk::gemm::fp8_gemm_kernel<4096u, 7168u, 128u, 128u, 128u, 5u, ...>(
    __nv_bfloat16*, float*, int*, unsigned int, unsigned int,
    CUtensorMap_st, CUtensorMap_st, CUtensorMap_st, CUtensorMap_st)
```

两个可验证的细节:其一,模板实参中的 `4096u` 与 `7168u` 恰好对应 [train.json:L97653](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97653) traceName 里的 `seqlen4096` 与 `hidden7168`(哪个是 M/N/K 不逐一断言,待确认);其二,签名末尾的**四个 `CUtensorMap_st`** 是 TMA 张量描述符的 类型——TMA 是 Hopper(SM 9.0)的硬件特性,这就解释了练习 3-3 的「T4 上没有 FP8 GEMM」。这些内核对应的实现在 [DeepGEMM](https://github.com/deepseek-ai/DeepGEMM)(如前所述,README 未直接链接,映射据命名空间推断,待确认)。

**(4) 延伸:注意力内核家族。** train 的 `flash::FlashAttnBwd`(u3-l1 实测 4 个、3.6~3.9ms)、prefill 的 `compute_attn_ws`(122 次,u2-l4)、decode 的 `flash_fwd_splitkv_mla` 两段式(34.9%,u2-l6)构成三场景注意力图谱;其中解码 MLA 内核与 [deepseek-ai/FlashMLA](https://github.com/deepseek-ai/FlashMLA) 仓库的解码 MLA 内核命名风格一致(具体对应关系待确认),可作为本讲之后的延伸阅读。

#### 4.3.4 代码实践(源码阅读型)

1. **实践目标**:亲手在三份轨迹中把三个家族的证据找齐,制作自己的「证据-仓库」映射表。
2. **操作步骤**:
   - 用 u1-l4 学会的方法统计 `train.json` 中名称含 `dpsk::ep::`、`dpsk::gemm::`/`dpsk::grouped_gemm::` 的 kernel 事件数(预期锚点:DeepEP 36 个、grouped_gemm 家族含 transpose/GemmKernel 若干,u3-l1 实测);
   - 用文本检索定位证据行(本文给出的锚点可直接跳转核对);
   - 在 `decode.json` 上**改用 JSON 解析**(单行文件,文本工具失真,u3-l1 的教训)确认 `dispatch_ll` 存在且 `dispatch`(normal 版)不存在;
   - 把结果填进 4.3.1 的表格,并为每行写一句「该仓库解决了什么问题」。
3. **需要观察的现象**:三个家族在 train 中分别落在 stream 27(通信)与 stream 7(计算);decode 中 ll 内核与计算同流。
4. **预期结果**:与 u2-l3/u2-l5/u3-l1 的实测数字一致(DeepEP 36 个占 46.5%;ll 内核各 235 次占 19.0%)。
5. 若你想更进一步:在 DeepEP 仓库的 README 里找到 normal 与 low-latency 两种模式的说明,与轨迹形态逐条对照(待本地验证)。

#### 4.3.5 小练习与答案

**练习 1**:README 只链接了 dualpipe 与 DeepEP,我们凭什么把 `dpsk::gemm::fp8_gemm_kernel` 归给 DeepGEMM?
**答案**:凭内核命名空间(`dpsk::gemm::`)与 DeepGEMM 仓库公开的功能定位(Hopper FP8 稠密/分组 GEMM)的吻合。但这是**推断**而非 README 声明,所以本讲一直标注「待确认」——证据等级要分清。

**练习 2**:`CUtensorMap_st` 出现在内核签名里说明了什么?
**答案**:该内核使用了 TMA(Tensor Memory Accelerator)传输张量,这是 Hopper 架构的特性;顺带推出该内核无法在图灵/安培等旧卡上运行,与仓库 deviceProperties 的 H800(computeCapability 9.0)一致。

**练习 3**:如果未来 DeepSeek 发布新轨迹,里面 `dispatch_ll` 被 `dispatch_llv2` 取代,你怎么最快确认它属于 DeepEP 体系?
**答案**:三步:查 README 是否更新了链接;查内核命名空间是否仍是 `dpsk::ep::`;查行为特征(RDMA 发出后释放 SM 的三段式时长分布是否保留,u2-l5 的方法)。命名空间 + 行为指纹比名字本身更稳。

## 5. 综合实践

**任务:自制一条「通信-计算重叠」轨迹,并量化它的重叠率**——把本讲(torch.profiler 采集、导出)与 u3-l1(聚合分析)、u3-l2(区间求交)串成完整闭环,亲手复现仓库的核心主题。

1. **实践目标**:在单卡上用双流制造「侧流拷贝(模拟通信)+ 主流 GEMM(模拟计算)」的时间线,导出轨迹,计算重叠率,并与 train(96.2%)/prefill(91.2%)对照。
2. **操作步骤**(示例代码):

   ```python
   #!/usr/bin/env python3
   # 示例代码:overlap_demo.py —— 双流重叠的最小复现
   import torch
   from torch.profiler import profile, ProfilerActivity, record_function

   assert torch.cuda.is_available(), "本实践需要 GPU(Colab 即可)"
   side = torch.cuda.Stream()                      # 侧流:模拟通信流
   x  = torch.randn(4096, 4096, device="cuda")
   w  = torch.randn(4096, 4096, device="cuda")
   src, buf = torch.randn(64 << 20, device="cuda"), torch.empty_like(src)  # 256MB

   with profile(activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA]) as prof:
       with record_function("overlap_window"):     # 给分析窗口打点
           for _ in range(20):
               with torch.cuda.stream(side):       # 侧流大拷贝 ≈ 通信
                   buf.copy_(src, non_blocking=True)
               for _ in range(8):                  # 主流密集 GEMM ≈ 计算
                   y = x @ w
       torch.cuda.synchronize()

   prof.export_chrome_trace("overlap_trace.json")
   ```

   随后:

   ```bash
   python3 analyze_trace.py overlap_trace.json 10
   ```

3. **需要观察的现象**:chrome://tracing 中 GPU 轨道出现两条 stream 线,拷贝内核(`Memcpy DtoD` 类事件)与 GEMM 内核在时间上**交叠**;`analyze_trace.py` 的 Top-10 里两类内核都上榜。
4. **预期结果**:对「拷贝内核区间集合 A、GEMM 内核区间集合 B」套用 u3-l2 的区间合并与求交,按通信侧口径计算

   \[
   R_{\text{overlap}} \;=\; \frac{\displaystyle\sum_{a \in A,\, b \in B} |\,a \cap b\,|}{\displaystyle\sum_{a \in A} |a|}
   \]

   应得到一个明显大于 0 的重叠率(具体数值取决于卡的带宽与算力之比,待本地验证)。再对照 train.json 的 96.2%(DualPipe,u3-l3):你会发现自己用 30 行代码造出的形态,与大集群上 DualPipe 的工程实现,在「双流并行」这个本质上是同一件事——差别只在通信的真身是 RDMA all-to-all 还是本地拷贝。
5. **验收标准**:轨迹能被 chrome://tracing 打开;`analyze_trace.py` 不崩溃且能区分两条流;能报出一个重叠率数字并写三句话解释它与 train/prefill 的异同。

## 6. 本讲小结

- **轨迹是采集参数的指纹**:`ProfilerStep#1` 证明用了 schedule+`prof.step()`([train.json:L14416-L14422](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L14416-L14422)),`1F1B` 等 42 条注解来自 `record_function`,`Input Dims` 全文 0 次证明未开 `record_shapes`。
- **自己会造,才算读懂格式**:`profile(activities=..., record_shapes=...)` + `export_chrome_trace` 产出的 JSON 与仓库三份轨迹同构;差异(无 `dpsk::` 内核、无分布式通信、deviceProperties 是本机卡)恰好勾勒出「单卡小模型 vs EP64/32/128 集群」的边界。
- **对照要用同一把尺子**:u3-l1 的 `analyze_trace.py` 对自制轨迹同样成立——`ph=="X"` 才计时、占比只在同一 `cat` 内算、按 `args.stream` 分流,这些口径与轨迹来自谁的集群无关。
- **证据分级,不编造映射**:DualPipe 与 DeepEP 有 README 显式链接([L11](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L11)、[L30](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L30));DeepGEMM 靠 `dpsk::gemm` 命名空间推断并已标注待确认;模板参数 `4096u/7168u` 与 traceName、`CUtensorMap_st` 与 Hopper TMA 的互证是免费的交叉验证。
- **收官视角**:整个仓库只有一个主题——通信与计算如何互相填缝;你现在既能在真实轨迹里测出它(u3-l2/u3-l3),也能在三十行代码里复现它的骨架(第 5 节)。

## 7. 下一步学习建议

本讲是学习手册的最后一讲,后续学习以「向外延伸」为主:

- **读实现**:带着轨迹证据去读三个仓库——[DualPipe](https://github.com/deepseek-ai/dualpipe) 的分块调度代码(对照 10 类注解)、[DeepEP](https://github.com/deepseek-ai/DeepEP) 的 normal/low-latency 两种模式(对照 stream 27 毫秒级内核与 stream 7 微秒级三段式)、[DeepGEMM](https://github.com/deepseek-ai/DeepGEMM) 的 FP8/grouped GEMM(对照 `fp8_gemm_kernel` 与 `grouped_gemm::GemmKernel`);解码 MLA 可延伸至 [FlashMLA](https://github.com/deepseek-ai/FlashMLA)(对应关系待确认)。
- **读论文**:DeepSeek-V3 技术报告中的 DualPipe 与 FP8 训练章节,是这三份轨迹的「设计文档」;读完再回看 `1F1B` 注解,理解会深一层。
- **继续练手**:把第 5 节的双流实验升级——改变拷贝大小与 GEMM 次数的比例,画出重叠率随负载比变化的曲线;或用 `schedule` 采集 20 个 step,复现 `ProfilerStep#N` 序列并统计每步耗时分布。
- **回读仓库**:用你此刻更锋利的工具链重走一遍 u3-l3 的三轨迹对比表,验证此前标注「待本地验证」的数字,把本手册的实测锚点补全成你自己的测量结果。
