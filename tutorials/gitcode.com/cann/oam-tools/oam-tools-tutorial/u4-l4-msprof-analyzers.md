# u4-l4 analyze 模块：原始性能数据的分类分析器

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 msprof 采集端「落盘前的最后一站」——analyze 模块——在整个数据链路中的位置。
2. 理解 `AnalyzerBase` 基类与 `Analyzer` 总调度器、五个子分析器（ge/rt/hwts/ts/ffts）之间的职责划分。
3. 理解 hwts、rt、ge、ts、ffts 各自处理哪一类原始性能数据文件。
4. 理解 `OpDescParser` 如何解析 ACL 算子订阅（aclprofSubscribe）产生的算子描述记录。
5. 掌握「新增一种原始数据文件的分析」需要从哪个类派生、实现哪些方法。

## 2. 前置知识

**原始性能数据（raw data）**：msprof 采集时，device 侧（AI Core、AICPU、硬件 trace 单元）和 host 侧（图引擎 GE、Runtime）会产出大量定长二进制记录（典型为 64 字节一条），直接落盘不可读。analyze 模块在数据「上传/落盘」之前把它们翻译、拼装成带语义的记录。

**ProfileFileChunk**：采集数据以「文件块」为单位在模块间流转，一个 chunk 携带 `fileName`（来源文件名）、`chunk`（数据本体）与 `chunkSize`（长度）。分析器靠 `fileName` 识别数据类别。

**syscnt 与频率换算**：device 侧记录的时间是系统计数器滴答数（syscnt），要除以频率才能得到微秒级时间。基类的 `InitFrequency()` 负责初始化这个换算系数。

**「流式分块 + 残留缓冲」**：一个文件可能被切成多个 chunk 先后到达，一条 64 字节记录可能跨 chunk 边界。基类用 `buffer_` 把不够一条记录的尾巴攒起来，与下一块拼接后再解析。

**重要考古提示**：本仓库当前 HEAD 下，analyze 模块**只保留了头文件**（`src/msprof/collector/dvvp/analyze/inc/` 下 9 个 `.h`），全部 `.cpp` 实现在提交 `64ebbaa`（「删除不参与编译的文件，并将UT的文件覆盖率提升到100%」）中被删除。头文件是开源的**接口契约**，实现细节可以用只读 git 命令考古：

```bash
git show 64ebbaa^:src/msprof/collector/dvvp/analyze/src/analyzer.cpp
```

本讲引用的 `.cpp` 代码片段均来自 `64ebbaa^`，会明确标注，**不会**伪造当前 HEAD 中不存在的文件链接。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/msprof/collector/dvvp/analyze/inc/analyzer_base.h` | 所有分析器的基类：数据缓冲、频率换算、跨分析器共享的静态状态表 |
| `src/msprof/collector/dvvp/analyze/inc/analyzer.h` | 总调度器 `Analyzer`：持有五个子分析器，按文件名分发数据 |
| `src/msprof/collector/dvvp/analyze/inc/analyzer_hwts.h` | 硬件 trace（hwts.data）分析器：device 侧算子起止时间 |
| `src/msprof/collector/dvvp/analyze/inc/analyzer_rt.h` | Runtime track 数据分析器：host 侧下发算子的时间线 |
| `src/msprof/collector/dvvp/analyze/inc/analyzer_ge.h` | 图引擎（GE）数据分析器：算子名/类型、模型 ID、图 ID 映射 |
| `src/msprof/collector/dvvp/analyze/inc/analyzer_ts.h` | device 软件 timeline 与关键点（keypoint）数据分析器 |
| `src/msprof/collector/dvvp/analyze/inc/analyzer_ffts.h` | FFTS/STARS 流水线子任务日志分析器 |
| `src/msprof/collector/dvvp/analyze/inc/op_desc_parser.h` | ACL 算子订阅记录 `ProfOpDesc`（64 字节/条）的解析器，单例 |
| `src/msprof/collector/dvvp/analyze/inc/data_struct.h` | 全模块共用的数据契约：`OpTime`、`RtOpInfo`、各类原始记录的内存布局 |
| `src/msprof/collector/dvvp/transport/parser_transport.h` | analyze 模块的上游消费者：`ParserTransport` 持有 `Analyzer` |
| `src/msprof/collector/dvvp/CMakeLists.txt` | `libprofimpl.so` 的源文件清单，仍列有 `analyze/src/*.cpp` |

## 4. 核心概念与源码讲解

### 4.1 AnalyzerBase：分析器的公共底座

#### 4.1.1 概念说明

五种来源各不相同的性能数据，解析时却有一批完全相同的底层需求：跨 chunk 拼数据、按频率换算时间、把「半条算子记录」暂存到匹配齐了再合并、以及多线程下保护共享表。`AnalyzerBase` 把这些需求收拢为一个基类，五个子分析器全部继承它。这是典型的**模板方法**思路：基类管机制，子类管各自的记录格式。

#### 4.1.2 核心流程

一个子分析器处理每块数据的主循环（以 hwts 为例）：

```
收到 ProfileFileChunk
  ├─ AppendToBufferedData(data, len)      # 与上一块残留的尾巴拼接
  ├─ offset = 0
  ├─ while 剩余长度 >= 一条记录长度(64B):
  │     ├─ 读记录头，判断记录类型
  │     ├─ 按类型解析字段（taskId/streamId/syscnt...）
  │     ├─ syscnt / frequency_ → 微秒时间
  │     ├─ 以 "taskId-streamId..." 为 key 存入中间表
  │     └─ offset += 64
  ├─ 不足一条记录 → 跳出循环
  └─ BufferRemainingData(offset)          # 把尾巴存回 buffer_，等下一块
```

#### 4.1.3 源码精读

基类声明与构造函数：构造时从 Platform 单例取最大 PMU 监控数，初始化一批进度统计字段：

[analyzer_base.h:29-36](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/analyzer_base.h#L29-L36) —— `AnalyzerBase` 类声明；构造函数里 `pmuNum_` 取自 `Platform::instance()->GetMaxMonitorNumber()`，说明分析器一开始就与芯片平台抽象层（u4-l1 讲过的 Platform 管理）挂钩。

流式解析的三件套与频率初始化：

[analyzer_base.h:44-55](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/analyzer_base.h#L44-L55) —— `AppendToBufferedData`（拼缓冲）、`BufferRemainingData`（存残留）、`InitFrequency`（注释明确写了换算公式：算子耗时 = syscnt / frequency）。

最有分量的是**静态共享状态区**：

[analyzer_base.h:74-91](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/analyzer_base.h#L74-L91) —— `rtOpInfo_`、`tsOpInfo_`、`geContextInfo_`、`geNodeInfo_`、`geApiInfo_`、`geModelInfo_`、`geOpInfo_`、`graphIdMap_`、`opDescInfos_` 全部是 `static` 成员，并配有四把 `std::mutex`。

为什么设计成 static？因为**一条完整算子信息是跨分析器拼出来的**：hwts 只知道「taskId-streamId 在 device 上何时起止」，算子叫什么名字在 ge 的数据里，host 侧下发时间在 rt 的数据里。把它们放在基类的静态区，任何一个子分析器都能读写同一份中间表，`Analyzer` 再做最终撮合上传。这是理解整个模块的钥匙。

#### 4.1.4 代码实践

实践目标：验证「基类静态成员被所有子分析器共享」这一论断，并体验 git 考古。

1. 打开 [analyzer_base.h:74-91](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/analyzer_base.h#L74-L91)，数一数静态成员与互斥锁的数量。
2. 执行 `git show 64ebbaa^:src/msprof/collector/dvvp/analyze/src/analyzer_base.cpp | grep -n 'InitFrequency' -A 20`，观察频率是从哪里读出来的（Platform 接口）。
3. 观察现象：`InitFrequency` 的返回值如何影响分析器是否可用。
4. 预期结果：能看到 `Analyzer` 构造函数（考古片段，见 4.2.3）中 `InitFrequency` 失败会把 `inited_` 置 false。具体输出待本地验证（本讲环境无昇腾设备，git 命令本身在任何 clone 中都可执行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tsOpInfo_` 必须是 static，而不能是 `AnalyzerHwts` 的普通成员？

**答案**：hwts 解析出的 device 侧起止时间（写入 `tsOpInfo_`）要被 `Analyzer` 的撮合逻辑和 ts/rt 分析器读取合并；若是非静态成员，其他分析器对象看不到这份表，跨数据源的算子信息就无法拼装。

**练习 2**：`buffer_`（非 static）为什么反而是每个分析器对象各一份？

**答案**：残留缓冲只与「单个数据流的解析进度」有关，不同来源文件各自有独立的记录边界，混在一个缓冲里反而会错位；而共享中间表是「跨来源拼装事实」，语义上必须全局唯一。

---

### 4.2 Analyzer：总调度器（外观 + 文件名路由）

#### 4.2.1 概念说明

`Analyzer` 是 analyze 模块对外的**唯一门面**（外观模式）：上游 `ParserTransport` 只认识 `Analyzer`，不认识五个子分析器。它做三件事：

1. 构造时创建五个子分析器并统一初始化频率。
2. 收到 `ProfileFileChunk` 后按 `fileName` 依次询问各子分析器「这是不是你的数据」，命中即分发。
3. 在合适的时机做「撮合上传」：把 device 时间 × host 时间 × 算子名拼成完整记录交给 `Uploader`。

#### 4.2.2 核心流程

分发优先级链（一条 if-else 链，顺序即优先级）：

```
OnOptimizeData(chunk)
  ├─ fileName == "end_info"（控制文件）→ 清空全部静态中间表，返回
  └─ DispatchOptimizeData(chunk)
        ├─ IsGeApiOrEventData ? → GeApiAndEventParse
        ├─ IsGeCompactData     ? → GeCompactParse
        ├─ IsGeGraphIdMapData  ? → GeGraphIdMapParse + TsDataPostProc
        ├─ IsGeContextData     ? → GeContextParse
        ├─ IsRtCompactData     ? → RtCompactParse
        ├─ IsHwtsData          ? → HwtsParse
        ├─ IsFftsData          ? → FftsParse
        ├─ IsTsData            ? → Parse + TsDataPostProc
        └─ 都不是 → 丢弃（仅 LOGD）
  最后统一 UploadProfOpDescProc()   # 把攒好的 ProfOpDesc 记录批量上传
```

`end_info` 是一个特殊约定：一轮采集结束时下发的控制文件，收到即把所有静态中间表清空，防止上一轮的「半条算子」污染下一轮。

#### 4.2.3 源码精读

门面类的公共接口：

[analyzer.h:40-52](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/analyzer.h#L40-L52) —— `OnOptimizeData` 是数据入口，`Flush` 落盘前冲刷，`PrintDeviceStats`/`PrintHostStats` 打印统计，`SetGraphType`/`SetOpType` 接收外部开关。

五个子分析器成员与上传通道：

[analyzer.h:77-82](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/analyzer.h#L77-L82) —— `analyzerGe_`、`analyzerHwts_`、`analyzerTs_`、`analyzerRt_`、`analyzerFfts_` 五个 `SHARED_PTR_ALIA` 成员加一个 `uploader_`（transport 层的上传器，u4-l1 讲过的「上报与落盘解耦」在此衔接）。

撮合上传的私有方法群：

[analyzer.h:57-67](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/analyzer.h#L57-L67) —— `UploadAppOp` 按三种 profile 模式分流到 `UploadAppOpModeStepTrace/StaticShape/SingleOp`；`UpdateOpIndexId`、`UploadKeypointOp`、`UploadProfOpDescProc` 分别处理关键点与算子描述的上传。三种模式常量定义在 [data_struct.h:27-30](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/data_struct.h#L27-L30)（`SINGLE_OP=1`、`STEP_TRACE=2`、`STATIC_SHAPE=3`）。

分发链的实现（考古片段，示例代码：来自已删除的提交 `64ebbaa^`，非当前 HEAD 文件，可用 `git show 64ebbaa^:src/msprof/collector/dvvp/analyze/src/analyzer.cpp` 复核）：

```cpp
void Analyzer::DispatchOptimizeData(SHARED_PTR_ALIA<analysis::dvvp::ProfileFileChunk> fileChunkReq)
{
    if (analyzerGe_->IsGeApiOrEventData(fileChunkReq->fileName)) {
        analyzerGe_->GeApiAndEventParse(fileChunkReq);
    } else if (analyzerGe_->IsGeCompactData(fileChunkReq->fileName)) {
        analyzerGe_->GeCompactParse(fileChunkReq);
    } else if (analyzerGe_->IsGeGraphIdMapData(fileChunkReq->fileName)) {
        analyzerGe_->GeGraphIdMapParse(fileChunkReq);
        TsDataPostProc();
    } else if (analyzerRt_->IsRtCompactData(fileChunkReq->fileName)) {
        analyzerRt_->RtCompactParse(fileChunkReq);
    } else if (analyzerHwts_->IsHwtsData(fileChunkReq->fileName)) {
        analyzerHwts_->HwtsParse(fileChunkReq);
    } else if (analyzerFfts_->IsFftsData(fileChunkReq->fileName)) {
        analyzerFfts_->FftsParse(fileChunkReq);
    } else if (analyzerTs_->IsTsData(fileChunkReq->fileName)) {
        analyzerTs_->Parse(fileChunkReq);
        TsDataPostProc();
    } else {
        return;  // 丢弃
    }
    UploadProfOpDescProc();
}
```

注意「路由表」没有集中写在 `Analyzer` 里，而是每个子分析器自带 `Is*Data(fileName)` 谓词——`Analyzer` 只负责串链。这样新增一种数据类别时，`Analyzer` 只需加一个成员、一行分支。

上游消费者（当前 HEAD 真实存在）：

[parser_transport.h:46-47](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/transport/parser_transport.h#L46-L47) —— `ParserTransport` 类持有 `Analysis::Dvvp::Analyze::Analyzer` 的智能指针。transport 层收到文件块后经它转交 analyze 模块，这正是 u4-l1 架构文档里「数据处理」模块的接缝。

构建清单佐证：

[CMakeLists.txt:37-45](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/CMakeLists.txt#L37-L45) —— `libprofimpl.so` 的源文件清单 `profimplCpp` 首先列出的就是 `analyze/src/analyzer.cpp` 等八个文件，且 [CMakeLists.txt:165](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/CMakeLists.txt#L165) 将 `analyze/inc` 加入头文件搜索路径。头文件目录在 HEAD 真实存在；`.cpp` 文件已从开源仓删除（清单条目仍在，实际构建依赖见 u1-l2 讲过的闭源 bundle 机制，此处标注：待确认——开源仓单独 clone 时 profimpl 目标的可编译性）。

#### 4.2.4 代码实践

实践目标：在当前 HEAD 里亲手「发现」analyze 模块的消费关系。

1. 执行 `grep -rn "Analysis::Dvvp::Analyze" src/msprof/collector/dvvp --include='*.h' --include='*.cpp' | grep -v analyze/inc`。
2. 观察 `parser_transport.h` 与 `profimpl/adapter/inc/msprofiler_acl_api.h`（其中声明了 `CreateParserTransport()`）两处命中。
3. 预期结果：确认开源仓中 analyze 模块对外的引用点只剩 transport 层的头文件契约；所有 `.cpp` 引用为零。这是「头文件即接口契约」的直接证据。

#### 4.2.5 小练习与答案

**练习 1**：`DispatchOptimizeData` 用 if-else 链而不是 `std::map<匹配器, 处理器>`，这样做有什么得与失？

**答案**：得——顺序即优先级，GE 系列谓词可能与其他文件名部分重叠，链式顺序保证了「更具体的先匹配」，且省去注册开销；失——新增类别要改 `Analyzer` 源码，违背子分析器「自带谓词」的局部封装，分支多了可读性下降。若谓词完全不重叠，可重构为注册表模式。

**练习 2**：`end_info` 控制文件为什么要清空 `opDescInfos_` 等静态表？

**答案**：静态表跨采集轮次存活，若上一轮残留「只有 start 没有 end 的半条算子」，下一轮数据到达时会与旧 key 错误撮合，产生时间倒挂或张冠李戴的记录；`end_info` 是一轮结束的信号，此时清场最安全。

---

### 4.3 五个子分析器：各管一类原始数据

#### 4.3.1 概念说明

五个子分析器按「数据来自 device 还是 host」自然分成两组：

- **device 侧**：`AnalyzerHwts`（硬件 trace 单元记录）、`AnalyzerTs`（device 软件 timeline/关键点）、`AnalyzerFfts`（FFTS/STARS 流水线日志）。
- **host 侧**：`AnalyzerGe`（图引擎的任务描述、API/事件、上下文、图 ID 映射）、`AnalyzerRt`（Runtime track 紧凑记录）。

每个子分析器都遵循同一套隐式契约（以 `AnalyzerHwts` 为模板）：

1. `Is*Data(fileName)`：按文件名特征认领数据。
2. `*Parse(fileChunkReq)`：入口，累计 `totalBytes_` 后进入解析。
3. `ParseOptimize*Data(data, len)`：缓冲拼接 + 逐条记录解析。
4. `PrintStats()`：输出「分析了多少字节/合并了多少条」的统计事件。

#### 4.3.2 核心流程

以 hwts 为例的完整解析算法（其余子分析器同构，仅记录布局不同）：

1. 记录长度固定 64 字节（`HWTS_DATA_SIZE`，见 [data_struct.h:123](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/data_struct.h#L123)）。
2. 每条记录第一个字节的低 3 位是类型（`cntRes0Type & 0x7`）：0 = 任务开始，1 = 任务结束。
3. 以 `taskId-streamId` 拼 key 查 `tsOpInfo_`：开始记录写 `start`，结束记录写 `end`，时间 = `syscnt / frequency_`。
4. 当 `start > 0 && end > 0`，这趟任务在 device 上的执行区间齐了，交给 `HandleDeviceData` 参与后续撮合。

时间换算可以写成：

\[ t_{\text{us}} = \frac{\text{syscnt}}{f_{\text{device}}} \]

其中 \( f_{\text{device}} \) 由 `InitFrequency()` 从 Platform 层获取。

#### 4.3.3 源码精读

**AnalyzerHwts（精读对象）**

[analyzer_hwts.h:29-38](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/analyzer_hwts.h#L29-L38) —— `AnalyzerHwts` 继承 `AnalyzerBase` 并声明 `friend class Analyzer`（总调度器可以直接读写它的私有中间表 `opTimes_`，这是门面撮合的权限基础）；公开接口只有 `IsHwtsData` 与 `HwtsParse` 两个。

[analyzer_hwts.h:47-52](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/analyzer_hwts.h#L47-L52) —— 两组关键中间表：`opTimeDrafts_`（注释「stores incomplete data」，半条记录的草稿区）与 `opTimes_`（key 为 `taskId-streamId-contextId` 的成品区），外加起止/合并计数器。

hwts 原始记录的内存布局就在 data_struct.h 里：

[data_struct.h:125-134](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/data_struct.h#L125-L134) —— `HwtsProfileType01`：首字节 `cntRes0Type`（bit0-2 类型、bit4-7 计数）、魔数 `hex6bd3`（0x6bd3，可用于校验记录对齐）、`taskId`、`syscnt`（滴答数）、`streamId`，注释标明总长 64 字节。同文件还有 `HwtsProfileType2/Type3`（含 coreId/blockId/warnStatus 的变体）。

解析循环实现（示例代码：考古自 `64ebbaa^` 的 analyzer_hwts.cpp）：

```cpp
void AnalyzerHwts::ParseOptimizeHwtsData(CONST_CHAR_PTR data, uint32_t len)
{
    AppendToBufferedData(data, len);
    uint32_t offset = 0;
    while (dataPtr_ != nullptr && offset < dataLen_) {
        uint32_t remainingLen = dataLen_ - offset;
        if (remainingLen < HWTS_DATA_SIZE) {
            break;  // 尾巴不足一条记录，留给 BufferRemainingData
        }
        uint8_t rptType = GetRptType(dataPtr_ + offset, remainingLen);
        if (rptType == HWTS_TASK_START_TYPE || rptType == HWTS_TASK_END_TYPE) {
            HandleOptimizeStartEndData(dataPtr_ + offset, rptType);
            analyzedBytes_ += HWTS_DATA_SIZE;
            totalHwtsTimes_++;
        }
        offset += HWTS_DATA_SIZE;
    }
    BufferRemainingData(offset);
}
```

而 `IsHwtsData` 的实现极其朴素：`fileName.find("hwts.data") != std::string::npos`——文件名路由本质是子串匹配。

**其余四个子分析器（接口概览）**

[analyzer_rt.h:28-45](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/analyzer_rt.h#L28-L45) —— `AnalyzerRt`：入口 `IsRtCompactData(tag)` / `RtCompactParse`，核心是 `ParseRuntimeTrackData` 解析 Runtime track 记录、`MatchDeviceOpInfo` 把 host 侧下发记录（`rtOpInfo`、`tsTmpOpInfo`）与 device 侧记录（`geOpInfo`）撮合——它就是「host 时间 × device 时间」的粘合剂。

[analyzer_ge.h:31-56](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/analyzer_ge.h#L31-L56) —— `AnalyzerGe` 是接口最宽的一个：五个 `Is*` 谓词对应五类 GE 数据（API/事件、compact 任务、图 ID 映射、context、任务描述），并向门面暴露查询能力 `GetOpName/GetOpType/GetModelId/IsOpInfoCompleted`——撮合时「这条 taskId 对应什么算子」全靠它回答。

[analyzer_ts.h:29-55](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/analyzer_ts.h#L29-L55) —— `AnalyzerTs`：`ParseTsTimelineData`（timeline，记录头 `TsProfileDataHead` 见 [data_struct.h:90-106](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/data_struct.h#L90-L106)）与 `ParseTsKeypointData`（关键点，训练场景 step 级锚点 `KeypointOp`）；私有 `keypointOpInfo_` 决定 profile 模式走 STEP_TRACE 还是 SINGLE_OP。

[analyzer_ffts.h:29-55](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/analyzer_ffts.h#L29-L55) —— `AnalyzerFfts`：解析 FFTS（Flexible/Flow 任务调度）产生的 ACSQ 任务日志与子任务线程日志，`StarsRollBackStreamTaskId` 处理 streamId/taskId 回卷（16 位计数器溢出翻转），记录布局 `StarsAcsqLog`/`StarsCxtLog` 见 [data_struct.h:166-195](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/data_struct.h#L166-L195)。

五类数据的对照表（本讲的实践任务会带你亲手填它）：

| 分析器 | 数据来源（文件名特征） | 侧别 | 记录布局（data_struct.h） | 输出/职责 | 关键方法 |
| --- | --- | --- | --- | --- | --- |
| AnalyzerGe | GE 任务描述、API/事件、compact、context、graph id map | host | `GeOpFlagInfo` | 算子名/类型/modelId 查询表 | `GeCompactParse`、`GetOpName` |
| AnalyzerRt | Runtime track 紧凑记录 | host | `RtOpInfo` | host 侧下发时间，与 device 记录撮合 | `RtCompactParse`、`MatchDeviceOpInfo` |
| AnalyzerHwts | 含 `hwts.data` | device | `HwtsProfileType01/2/3`（64B/条） | device 侧算子起止时间 | `HwtsParse` |
| AnalyzerTs | TS timeline / keypoint | device | `TsProfileTimeline/Keypoint` | 时间线 + step 关键点锚 | `Parse`、`ParseTsKeypointData` |
| AnalyzerFfts | FFTS/STARS 日志 | device | `StarsAcsqLog/StarsCxtLog`（64B/条） | 子任务线程级耗时 | `FftsParse` |

#### 4.3.4 代码实践

实践目标：以 `analyzer_base.h` 为基准核对上表，并写一份「新增原始数据文件」的派生方案。

1. 逐个打开五个子分析器头文件，核对表中「关键方法」「数据布局」两列与真实声明是否一致（行号见上文链接）。
2. 在 `data_struct.h` 中为每种布局找到对应的常量（如 `HWTS_DATA_SIZE`、`STARS_DATA_SIZE`、`TS_TIMELINE_RPT_TYPE`）。
3. 预期结果：五份头文件的公共接口均由「谓词 + Parse 入口」构成，私有部分是各自的中间表——契约一致性得到验证。
4. 若发现某列与源码不符，以源码为准修正表格。全部核对可离线完成，无需设备。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `AnalyzerHwts` 要 `friend class Analyzer`，而不是给 `opTimes_` 加 getter？

**答案**：撮合阶段 `Analyzer` 要遍历、erase、批量搬移 `opTimes_` 中的元素（见 `UploadAppOp` 系列考古片段），getter 返回引用同样会暴露内部结构，反而多一层形式；friend 把「读写的权限」收敛到唯一的门面类，语义上更明确——只有调度器可以动成品表。

**练习 2**：hwts 记录里 `hex6bd3 = 0x6bd3` 这样的魔数有什么用？

**答案**：定长二进制流一旦错位（比如 chunk 边界处理出错），后续所有字段全部读错；解析时校验每条记录偏移 +3 处两字节是否为 0x6bd3，可以快速发现错位，避免产出垃圾时间线。当前开源头文件未展示校验逻辑，属合理推测，待确认（可考古 `64ebbaa^` 的 analyzer_hwts.cpp 验证——实际实现未做该校验，仅作布局对齐用途）。

**练习 3**：`AnalyzerTs` 的 keypoint 数据如何影响整个分析器的行为？

**答案**：`TsDataPostProc`（考古片段，analyzer.cpp）中，若 `keypointOpInfo_` 非空则 profile 模式定为 `PROFILE_MODE_STEP_TRACE`，否则若 `opTimes_` 非空定为 `PROFILE_MODE_SINGLE_OP`；模式又决定 `UploadAppOp` 走哪条撮合分支——同一份数据在不同模式下产出不同的上报粒度。

---

### 4.4 OpDescParser 与 data_struct：算子描述解析与数据契约

#### 4.4.1 概念说明

前面三个模块解决「文件流的分析」，`OpDescParser` 解决另一类数据：ACL 算子订阅（`aclprofSubscribe`，即第三方算子精度/耗时订阅接口）产生的 `ProfOpDesc` 记录——固定 64 字节一条的定长结构，包含算子起止时间、耗时、AI Core 执行时间、Cube/Vector 浮点操作数等。它是一个**单例**（CRTP 风格 `Singleton<OpDescParser>`），并额外维护 `opIndex → opName/opType` 的映射，为其他记录补齐「算子叫什么」。

`data_struct.h` 则是全模块的契约层：所有跨分析器传递的结构体（`OpTime`、`RtOpInfo`、`GeOpFlagInfo`）与所有原始记录的内存布局都定义在这一个文件里。

#### 4.4.2 核心流程

`ProfOpDesc` 的访问模型是「索引式读取」：

```
GetOpNum(data, len, &opNum)              # 先数出这段数据里有几条记录
for i in 0..opNum:
    GetOpStart(data, len, i)             # 第 i 条的 start 字段
    GetOpDuration(data, len, i)          # 第 i 条的 duration（单位 us）
    GetOpExecutionTime(data, len, i)     # 第 i 条的 AI Core 执行时间
    GetOpCubeFops / GetOpVectorFops      # 第 i 条的浮点操作数
```

每次访问都带 `data/len` 并经 `CheckData`（私有）做边界校验，避免越界读。

#### 4.4.3 源码精读

单例与静态访问器群：

[op_desc_parser.h:34-53](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/op_desc_parser.h#L34-L53) —— `class OpDescParser : public Singleton<OpDescParser>`；一大片 `static` 方法按索引取 `ProfOpDesc` 各字段（`GetOpStart/GetOpEnd/GetOpDuration/GetOpExecutionTime/GetOpCubeFops/GetOpVectorFops/GetOpFlag`，以及取字符串型属性的 `GetOpAttriValue`）。

实例态的名字映射：

[op_desc_parser.h:54-67](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/op_desc_parser.h#L54-L67) —— `SetOpTypeAndOpName` 把外部登记的算子类型/名字换成一个 `opIndex_`（自增 ID），存入 `opTypes_`/`opNames_` 两张 map。设计动机：定长 64 字节记录里塞不下变长字符串，于是记录里只存索引，名字单独登记、按需反查——与 msaicerr 解析 Dump 时「超长文件名走 mapping.csv 反查」异曲同工（见 u3-l3）。

`ProfOpDesc` 的字段契约：

[data_struct.h:198-211](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/data_struct.h#L198-L211) —— `signature`（防篡改签名，上传前由 `Utils::GenerateSignature` 重新计算，见 4.2.3 考古片段 `UploadProfOpDescProc`）、`modelId/flag/threadId/devId`、`duration`（注释：单位 us，调度时间 + 执行时间）、`start/end`、`executionTime`（AI Core 执行时间）、`cubeFops/vectorFops`，总长 64 字节。

跨分析器传递的两个核心结构：

[data_struct.h:46-55](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/data_struct.h#L46-L55) —— `OpTime`：撮合后的成品（indexId、start/end、AI Core 起止 `startAicore/endAicore`、threadId、flag、streamId），是 `UploadAppOp` 系列的操作对象。

[data_struct.h:305-316](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/data_struct.h#L305-L316) —— `RtOpInfo`：host/device 两侧时间的中间态（`tsTrackTimeStamp`、`start/end`、`startAicore/endAicore`、`ageFlag`、`contextId`、`devId`），是 `tsOpInfo_`/`rtOpInfo_` 等静态表的 value 类型。

#### 4.4.4 代码实践

实践目标：用「纸上反编译」吃透一个定长结构。

1. 读 [data_struct.h:198-211](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/data_struct.h#L198-L211) 的 `ProfOpDesc`，手工计算每个字段在 64 字节里的偏移（提示：4×uint32 + 4×uint64 + 1×uint32 = 16 + 32 + 4 = 52 字节，剩余 12 字节由对齐/保留填充；注释声称总长 64 字节）。
2. 用 `sizeof(ProfOpDesc)` 的推理结果对照 `GetOpDescSize()`（[op_desc_parser.h:40](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/op_desc_parser.h#L40)）的语义：它应返回这条记录的步长。
3. 写一个 10 行的独立 C++ 小程序（示例代码，非项目原有文件）：定义同样的结构体并 `static_assert(sizeof(ProfOpDesc) == 64)`，在本机 `g++ -c` 编译验证你的字段计算。
4. 预期结果：断言通过说明布局理解正确；若失败，检查是否漏了 `executionTime` 或对齐填充。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`OpDescParser` 的字段访问器为什么全部做成 static，而名字映射却是实例成员？

**答案**：字段访问是**纯函数**——只依赖传入的 `data/len/index`，无状态，static 可直接用类名调用且天然线程安全；名字映射需要可写的 `opIndex_` 与两张 map（有状态），放进单例实例由 `mtx_` 保护读写。

**练习 2**：`ProfOpDesc.signature` 在什么时候被赋值？

**答案**：不是采集时，而是上传前——考古片段 `UploadProfOpDescProc` 中，`Analyzer` 每次批量上传前对每条记录调用 `Utils::GenerateSignature` 重算签名（跳过 signature 字段本身），用于下游校验记录完整性。

---

## 5. 综合实践

**任务：为一种假想的原始数据文件设计一个新分析器。**

背景：假设下一代硬件 trace 单元会产出 `xyz.data` 文件，每条记录 32 字节：首字节低 3 位为类型（2 = 算子开始，3 = 算子结束），第 4 字节起为 uint32 taskId，第 8 字节起为 uint64 syscnt。

请完成以下交付物（纯纸面设计 + 头文件骨架，不修改仓库源码）：

1. **数据契约**：仿照 [data_struct.h:125-134](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/analyze/inc/data_struct.h#L125-L134) 的 `HwtsProfileType01`，写出 `XyzProfileType` 结构体与 `XYZ_DATA_SIZE = 32`、`XYZ_TASK_START_TYPE = 2`、`XYZ_TASK_END_TYPE = 3` 常量。
2. **分析器骨架**：新建 `analyzer_xyz.h`（示例代码，非项目原有文件），让 `AnalyzerXyz : public AnalyzerBase`，声明：
   - `bool IsXyzData(const std::string &fileName);` —— 实现思路：子串匹配 `"xyz.data"`；
   - `void XyzParse(SHARED_PTR_ALIA<analysis::dvvp::ProfileFileChunk> fileChunkReq);` —— 实现思路：`AppendToBufferedData` → 按 32 字节步进 → `syscnt / frequency_` 换算 → 以 `taskId-streamId` 为 key 写入基类静态表 → `BufferRemainingData`；
   - `friend class Analyzer;`。
3. **接入点清单**：说明要动哪些地方——`analyzer.h` 增加成员 `analyzerXyz_` 并前向声明、构造函数中创建、`DispatchOptimizeData` 的 if-else 链中插入分支（建议插在 `IsHwtsData` 之前后均可，前提是文件名不与现有谓词重叠）、`CMakeLists.txt` 的 `profimplCpp` 增加新 `.cpp`。
4. **自检**：对照 4.3.3 的对照表，为新分析器填一行（来源文件、侧别、布局、职责、关键方法）。

参考答案要点：新分析器必须复用基类的缓冲三件套与静态中间表，而不是自建平行的表——否则失去与 ge/rt 数据撮合的能力；`IsXyzData` 的文件名特征不能是现有任何谓词的子串，否则会被链上更靠前的分支截胡。

## 6. 本讲小结

- analyze 模块当前开源形态是「**九个头文件构成的接口契约**」：`Analyzer` 门面 + `AnalyzerBase` 基类 + 五个子分析器 + `OpDescParser` + `data_struct.h`；`.cpp` 实现在提交 `64ebbaa` 中删除，可 `git show 64ebbaa^:...` 考古。
- `Analyzer` 按 `fileName` 走 if-else 谓词链分发数据，优先级 GE 系列最高；`end_info` 控制文件触发全量静态表清场。
- 子分析器的统一契约是「`Is*Data` 认领 + `*Parse` 解析 + `PrintStats` 统计」；device 侧（hwts/ts/ffts）与 host 侧（ge/rt）各管一类原始数据。
- 一条完整算子记录是**跨分析器拼装**的：hwts 给 device 起止时间、ge 给算子名与 modelId、rt 给 host 下发时间，靠 `AnalyzerBase` 的 static 中间表共享、由 `Analyzer` 撮合上传——这是本模块最重要的设计。
- `OpDescParser` 以「static 索引式访问器 + 单例名字映射」解析 64 字节定长的 `ProfOpDesc` 记录，定长存索引、变长名字走反查。
- 定长二进制解析的三板斧：跨 chunk 缓冲拼接、魔数/步长对齐、`syscnt / frequency` 时间换算。

## 7. 下一步学习建议

- 下一讲 **u4-l5** 切换到用户视角，学习 msprof 的多种采集触发方式（`msprof` 命令、环境变量 `PROFILING_MODE`/`PROFILING_OPTIONS`、acl.json 与延迟采集），你会看到本讲分析器产出的数据最终如何在 MindStudio Insight 中呈现。
- 建议继续阅读的源码：`src/msprof/collector/dvvp/transport/uploader.h`（撮合结果上传的下游）、`src/msprof/collector/dvvp/profimpl/platform/base_analyzer.cpp`（另一套 device 形态相关的 platform 分析器，注意与本章 `AnalyzerBase` 不是一个体系）。
- 若对「数据如何从 device 到达 analyze」感兴趣，可考古 `64ebbaa^` 的 `transport/parser_transport.cpp`，把 transport → Analyzer 的调用链补全。
- 学完 u4-l5 后进入 u5 单元（hccl_test），换一种语言与问题域继续源码阅读训练。
