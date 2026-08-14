# TS Agent 任务调度代理与 esched 事件调度

## 1. 本讲目标

本讲是单元 6「SDK-driver 内核层与任务/故障调度」的第 4 篇，承接 u6-l1（内核态 `sdk_driver` 与 `kernel_adapt`）和 u6-l3（TRS 任务资源调度的 SQ/CQ 与 mailbox）。读完本讲你应该能够：

- 说清楚 **TS Agent（ts_agent）** 作为「任务调度代理驱动」在内核态承担什么职责，以及它为什么只出现在虚拟化场景。
- 理解 **vsq_worker（虚拟 SQ 工作队列）** 如何把虚拟机下发的任务逐条搬运、翻译、再投递到物理 SQ。
- 理解 **event_sched（事件调度）** 的「组（grp）— 线程（thread）— 事件（event）」三要素模型，以及提交 / 等待 / 应答（submit / wait / ack）的语义。
- 说明 **esched 适配层** 如何把用户态请求经字符设备 `/dev/event_sched` 送到内核，以及 esched 如何复用 u6-l3 讲过的 TRS SQ/CQ 数据面。
- 用一张图把 TS Agent（任务面）和 esched（事件面）在任务调度链路中的协作关系画出来。

---

## 2. 前置知识

在进入正文前，先用通俗的话对齐几个概念。已经学过 u6-l3 的读者可跳过前两条。

- **SQ / CQ（Submission / Completion Queue）**：设备上的环形队列。Host 把「要干的活」写成 SQE（提交元素）塞进 SQ，设备干完后把「完成情况」写成 CQE 塞进 CQ。这是昇腾驱动任务下发的数据面底座，详见 u6-l3。
- **虚拟化与 VF（Virtual Function）**：一张物理 NPU（PF）可以被 SR-IOV 切分成多个虚拟实例（VF），分给不同虚拟机使用。详见 u7-l5 的 vascend/vmng。本讲的 TS Agent 主要就是为 VF 场景服务的。
- **「虚拟 ID」与「物理 ID」**：虚拟机里的程序看到的是「虚拟 stream id / event id / model id」，互不干扰；但底层硬件只认「物理 id」。两者之间必须有一层翻译。这是本讲反复出现的核心动作。
- **用户态 / 内核态与字符设备**：esched 跑在用户态（编进 `libascend_hal.so`），它通过 `open("/dev/event_sched")` + `ioctl` 陷入内核；真正实现事件调度的是内核里的 esched 模块（`src/sdk_driver/esched/`）。这套「用户态门面 + 内核态实现 + ioctl 跨态」的套路与 SVM/HDC 完全一致。

> 一句话定位：**TS Agent 管「任务怎么翻译后投到物理队列」，esched 管「事件如何在主机/设备线程间同步」**，两者最终都落在 u6-l3 的 TRS SQ/CQ 数据面上。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下（行号与永久链接以当前 HEAD `e29d066` 为准）：

| 文件 | 所在层 | 作用 |
| --- | --- | --- |
| `src/sdk_driver/ts_agent/src/ts_agent_module.c` | 内核态 | ts_agent 模块入口：`init/exit`、PCI 设备表、向 trsdrv 注册回调 |
| `src/sdk_driver/ts_agent/src/ts_agent_vsq_worker.c` | 内核态 | 为每个 VF/TS/VSQ 创建工作队列，把 VSQ 处理投递到内核工作线程 |
| `src/sdk_driver/ts_agent/src/ts_agent_vsq_proc.c` | 内核态 | VSQ 环形队列遍历、任务类型分派、虚拟→物理 ID 翻译 |
| `src/sdk_driver/ts_agent/src/ts_agent_resource.c` | 内核态 | ID 翻译（stream/event/notify/model）、VSQ 信息查询的封装 |
| `src/sdk_driver/ts_agent/inc/ts_agent_common.h` | 内核态 | `vsq_base_info_t` 等结构、设备/VF/SQ 数量上限宏 |
| `src/sdk_driver/inc/trs/hvtsdrv_tsagent.h` | 接口 | ts_agent 与 trsdrv 之间的注册接口与回调函数表 |
| `src/ascend_hal/esched/comm/event_sched.c` | 用户态 | esched 核心：submit/wait/ack/subscribe/group 等 `halEsched*` 接口 |
| `src/ascend_hal/esched/comm/event_sched.h` | 用户态 | 核心数据结构（grp/thread/wait_info）与日志宏 |
| `src/ascend_hal/esched/comm/event_sched_app.c` | 用户态 | 上层「同步事件」封装（提交后等应答，类 RPC） |
| `src/ascend_hal/esched/comm/drv_event_proc.c` | 用户态 | 驱动内部「事件处理线程」：订阅并分发设备→主机异步消息 |
| `src/ascend_hal/esched/esched_adapt.c` | 用户态 | 适配层：attach/detach 引用计数、能力判定、时间换算 |
| `src/ascend_hal/esched/esched_topic_sqe.c` | 用户态 | 把 event 打包成 topic SQE，复用 TRS SQ/CQ 通道 |

---

## 4. 核心概念与源码讲解

### 4.1 ts_agent 任务调度代理总览与模块装配

#### 4.1.1 概念说明

「ts_agent」全称 Task Schedule Agent（任务调度代理）。要理解它，先看一个矛盾：

- 在**非虚拟化**（裸机）场景下，Runtime 直接把任务写进物理 SQ，设备直接消费，路径很短，不需要中间人。
- 在**虚拟化**（SR-IOV VF）场景下，每个虚拟机拥有自己的「虚拟 SQ（VSQ）」和「虚拟资源 ID」。虚拟机不能、也不应该直接操作物理硬件——否则多个虚拟机会互相踩踏。

ts_agent 就是插在「虚拟机的 VSQ」和「物理 SQ」之间的那个**代理**：它替虚拟机把 VSQ 里的任务读出来、把虚拟 ID 翻译成物理 ID、再投递到真正的物理队列。它是一个运行在 **Host 内核态** 的内核模块（`.ko`），与 u6-l3 的 trsdrv 配合工作。

为什么不直接让 trsdrv 干这件事？因为「翻译规则」与具体的虚拟机编排（vascend/vdavinci，见 u7-l5）强相关，把它独立成一个模块便于演进和按平台裁剪——这正是 u6-l1 讲过的「薄封装 + 业务子模块」分工思想的体现。

#### 4.1.2 核心流程

ts_agent 的装配遵循 Linux 内核模块的标准套路，但「真正的业务」并不在模块里直接执行，而是**注册一组回调给 trsdrv**，由 trsdrv 在合适的时机回调：

```text
insmod ts_agent.ko
   └─ ts_agent_init()
        ├─ init_task_convert_func()          // 建立任务类型→翻译函数 表
        ├─ tsagent_stream_id_to_sq_id_init() // 建 stream_id 映射表
        ├─ init_all_vf_work_ctx()            // 初始化 VF 工作上下文数组
        └─ hal_kernel_hvtsdrv_tsagent_register(&ops)  // 把回调表交给 trsdrv
             ops.tsagent_vf_create    ──┐
             ops.tsagent_vf_destroy     │  这些函数由 trsdrv 在
             ops.tsagent_vsq_proc       │  设备/VF 生命周期事件中回调
             ops.tsagent_trans_mailbox_msg ─┘
rmmod
   └─ ts_agent_exit() → 反注册 + destroy_all_vf_work_ctx()
```

注意源码里有两套注册路径，用 `CFG_SOC_PLATFORM_STARS` 宏二选一：STARS 平台走 `trs_sqcq_agent_ops_register`（按 SQE/CQE/MB 更新回调），其余平台（CLOUD/MINIV3）走 `hal_kernel_hvtsdrv_tsagent_register`。本讲以更通用的后者为主。

#### 4.1.3 源码精读

模块入口 `ts_agent_init` 完成建表与注册：

[src/sdk_driver/ts_agent/src/ts_agent_module.c:29-71](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/ts_agent/src/ts_agent_module.c#L29-L71) —— 初始化任务翻译表、VF 工作上下文，并把 `tsagent_vf_create` / `tsagent_vf_destroy` / `tsagent_vsq_proc` / `tsagent_trans_mailbox_msg` 四个回调注册给 trsdrv。

PCI 设备表决定内核把该模块绑定到哪些 NPU 设备（华为厂商 ID `0x19e5`）：

[src/sdk_driver/ts_agent/src/ts_agent_module.c:90-103](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/ts_agent/src/ts_agent_module.c#L90-L103) —— `g_ts_agent_tbl` 列出 `0xd801/0xd802/0xd105/0xd500/0xd803` 等设备 ID，并兼容若干第三方厂商 ID，`KA_MODULE_DEVICE_TABLE` 把它登记给 PCI 子系统。

模块声明通过 `ka_*` 宏（u6-l1 的 kernel_adapt 薄封装）导出：

[src/sdk_driver/ts_agent/src/ts_agent_module.c:105-111](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/ts_agent/src/ts_agent_module.c#L105-L111) —— `ka_module_init/exit` 注册加载/卸载函数，`KA_MODULE_LICENSE("GPL v2")` 声明协议。

回调函数表本身定义在共享头里，是 ts_agent 与 trsdrv 的「契约」：

[src/sdk_driver/inc/trs/hvtsdrv_tsagent.h:54-60](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/inc/trs/hvtsdrv_tsagent.h#L54-L60) —— `struct hvtsdrv_tsagent_ops` 列出四个函数指针，trsdrv 持有这张表就能在 VF 创建/销毁、VSQ 有新任务、需要转发 mailbox 时回调 ts_agent。

#### 4.1.4 代码实践

1. **实践目标**：看清「模块加载」与「业务回调注册」是两个分离的步骤，理解 ts_agent 是「被动响应」而非「主动轮询」。
2. **操作步骤**：
   - 打开 `src/sdk_driver/ts_agent/src/ts_agent_module.c`，对照 `ts_agent_init` 与 `ts_agent_exit`，列出 init 里做了哪些「建表/初始化」、哪些「注册回调」。
   - 打开 `src/sdk_driver/inc/trs/hvtsdrv_tsagent.h`，找到 `struct hvtsdrv_tsagent_ops` 与紧随其后的 `hal_kernel_hvtsdrv_tsagent_register` / `hal_kernel_hvtsdrv_sq_write` / `hal_kernel_hvtsdrv_resid_v2p` 等函数声明。这些「反向调用 trsdrv」的函数就是 ts_agent 把翻译结果回投到物理队列的通道。
3. **需要观察的现象**：init 中并没有任何「读 VSQ」的循环；所有读取动作都在 `tsagent_vsq_proc` 这个回调里，而该回调由 trsdrv 触发。
4. **预期结果**：你能用自己的话说明「ts_agent 加载时只做装配，真正干活靠 trsdrv 回调」。

#### 4.1.5 小练习与答案

- **练习 1**：`ts_agent_init` 在 STARS 平台和非 STARS 平台注册的回调分别是什么？为什么不同？
  - **答**：STARS 走 `trs_sqcq_agent_ops_register`，注册 `sqe_update/mb_update/cqe_update/device_init/device_uninit`（按 SQE/CQE/mailbox 更新粒度回调）；非 STARS 走 `hal_kernel_hvtsdrv_tsagent_register`，注册 `vf_create/vf_destroy/vsq_proc/trans_mailbox_msg`（按 VF 生命周期 + 整段 VSQ 处理回调）。差异源于两代芯片的调度模型不同——STARS 以 SQE 增量更新为核心，老平台以「整条 VSQ 交给代理处理」为核心。
- **练习 2**：为什么 PCI 设备表里要列多个设备 ID？
  - **答**：不同型号 NPU（以及不同板卡形态）的 PCI 设备 ID 不同，列全才能保证模块在多种硬件上都被正确加载绑定。

---

### 4.2 ts_agent_vsq_worker：虚拟 SQ 工作队列机制

#### 4.2.1 概念说明

注册好回调后，trsdrv 一旦发现「某个 VF 的某条 VSQ 里有新任务」，就会回调 `tsagent_vsq_proc`（最终走到 `schedule_vsq_work`）。但内核里直接在回调上下文做「遍历整条 VSQ + 逐条翻译 + 写物理 SQ」会阻塞调用者太久。于是 ts_agent 采用了 **「每条 VSQ 一个内核单线程工作队列（singlethread workqueue）」** 的设计：

- 回调里只做「轻量记账 + 把 work 投递到队列」，立刻返回；
- 真正的「遍历 + 翻译 + 回投」在内核工作线程里异步执行。

这避免了在 trsdrv 的中断/回调上下文里做重活，也保证了**同一条 VSQ 内的任务串行处理**（单线程队列的天然性质）。

#### 4.2.2 核心流程

VSQ 的数据结构按「设备 → VF → TS → VSQ」四级组织，每一级用一个数组下标定位：

```text
g_all_vf_worker[dev_id][vf_id][ts_id]  → vf_work_ctx_t
        └─ vsq_work_ctx_list[vsq_id]   → vsq_work_ctx_t
              ├─ vsq_base_info  // dev_id/vf_id/ts_id/vsq_id，供 work 反查
              ├─ proc_work      // ka_work_struct_t，挂到工作队列
              └─ wq             // 单线程工作队列
```

调度一次 VSQ 处理的流程：

```text
trsdrv 回调 tsagent_vsq_proc(id_inst, vsq_id, vsq_type, cmd_num)
   └─ schedule_vsq_work()
        ├─ 用 (dev_id, vf_id, ts_id, vsq_id) 定位到 vsq_work_ctx
        ├─ vsq_top_proc(...)        // 平台相关的预处理（当前实现为空壳直接返回 EOK）
        └─ ka_task_queue_work(wq, &proc_work)   // 投递，立刻返回

工作线程随后执行 proc_vsq_work()
   └─ ka_container_of 反查到 vsq_work_ctx
   └─ proc_vsq(&vsq_base_info)      // 见 4.2.3 的逐条处理
```

#### 4.2.3 源码精读

三级上下文结构与全局数组定义：

[src/sdk_driver/ts_agent/src/ts_agent_vsq_worker.c:23-43](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/ts_agent/src/ts_agent_vsq_worker.c#L23-L43) —— `vsq_work_ctx_t` 持有 `vsq_base_info`、`proc_work`、`wq`；`g_all_vf_worker` 用三维数组组织所有 VF 的工作上下文，`VF_WORK` 宏按下标取值。

为单条 VSQ 创建工作队列并初始化 work：

[src/sdk_driver/ts_agent/src/ts_agent_vsq_worker.c:108-135](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/ts_agent/src/ts_agent_vsq_worker.c#L108-L135) —— `create_vsq_work_ctx` 用 `dev_id/vf_id/ts_id/vsq_id` 拼出工作队列名（如 `tsa_<dev>_<vf>_<ts>_<vsq>`），调用 `ka_task_create_singlethread_workqueue` 建单线程队列，再用 `KA_TASK_INIT_WORK` 把 `proc_vsq_work` 绑到 `proc_work`。

回调入口把处理投递到队列：

[src/sdk_driver/ts_agent/src/ts_agent_vsq_worker.c:200-243](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/ts_agent/src/ts_agent_vsq_worker.c#L200-L243) —— `schedule_vsq_work` 校验 `vsq_id` 范围、刷新 `vsq_base_info`，调用 `vsq_top_proc` 后 `ka_task_queue_work` 把 work 入队。注意末尾的 `if/else` 日志：`queue_work` 返回真表示「成功入队」，返回假表示「该 work 已在队列中排队」，两种都是正常情况。

工作线程反查上下文并进入实际处理：

[src/sdk_driver/ts_agent/src/ts_agent_vsq_worker.c:100-106](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/ts_agent/src/ts_agent_vsq_worker.c#L100-L106) —— `proc_vsq_work` 用 `ka_container_of`（等价于 Linux 的 `container_of`）从 `work` 反算出所属 `vsq_work_ctx_t`，再调 `proc_vsq`。

`proc_vsq` 的实际逐条处理在另一个文件里，是本模块的核心：

[src/sdk_driver/ts_agent/src/ts_agent_vsq_proc.c:551-578](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/ts_agent/src/ts_agent_vsq_proc.c#L551-L578) —— `proc_vsq` 取 VSQ 的 head/tail，`head == tail` 说明空则退出，否则调 `proc_vsq_by_range` 处理 `[head, tail)` 区间，处理完会再 retry 一次（防止处理期间又有新任务）。

[src/sdk_driver/ts_agent/src/ts_agent_vsq_proc.c:494-549](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/ts_agent/src/ts_agent_vsq_proc.c#L494-L549) —— `proc_vsq_by_range` 是真正的「搬运+翻译+回投」循环：用 `memcpy_s` 从 VSQ 拷一个 task 槽 → `proc_task`（翻译）→ `hal_kernel_hvtsdrv_sq_write`（写到物理 SQ）→ 推进 `curr_head`，最后若有处理过则 `hal_kernel_hvtsdrv_sq_irq_trigger` 敲一次中断通知设备。注意循环里即使 `proc_task` 失败也**不 break**——注释解释：出错的任务仍要发给 TS，否则会丢失 CQ 上报、无法更新 VSQ head。

任务翻译分派与 ID 转换：

[src/sdk_driver/ts_agent/src/ts_agent_vsq_proc.c:446-492](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/ts_agent/src/ts_agent_vsq_proc.c#L446-L492) —— `proc_task` 按 `vsq_type`（`NORMAL_VSQCQ_TYPE` / `CALLBACK_VSQCQ_TYPE`）分派，普通任务转成 `ts_task_t` 后调 `convert_task` 做翻译。

[src/sdk_driver/ts_agent/src/ts_agent_vsq_proc.c:408-444](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/ts_agent/src/ts_agent_vsq_proc.c#L408-L444) —— `convert_task` 用函数指针表 `g_task_convert_fn[task->type]` 做按类型定制的翻译（先 custom，再 `convert_task_basic` 翻译 stream id），失败时打 `TS_TASK_INVALID_FLAG` 标记但仍继续。

ID 翻译最终落到资源模块，向 trsdrv 查询虚拟→物理映射：

[src/sdk_driver/ts_agent/src/ts_agent_resource.c:58-81](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/ts_agent/src/ts_agent_resource.c#L58-L81) —— `convert_virt_to_phy` 填好 `hvtsdrv_id_v2p`（含 `id_type`），调 `hal_kernel_hvtsdrv_resid_v2p` 让 trsdrv 给出物理 id。

[src/sdk_driver/ts_agent/src/ts_agent_resource.c:88-106](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/ts_agent/src/ts_agent_resource.c#L88-L106) —— `convert_stream_id` / `convert_event_id` / `convert_notify_id` / `convert_model_id` 都是对 `convert_virt_to_phy` 的薄封装，区别只在传入的 `id_type`（`TSDRV_STREAM_ID` / `TSDRV_EVENT_SW_ID` 等）。

#### 4.2.4 代码实践

1. **实践目标**：跟踪一条任务从「进入 VSQ」到「翻译后写入物理 SQ」的完整调用链。
2. **操作步骤**：
   - 从 `schedule_vsq_work`（`ts_agent_vsq_worker.c:200`）出发，跟踪到 `proc_vsq` → `proc_vsq_by_range` → `proc_task` → `convert_task`。
   - 在 `ts_agent_vsq_proc.c` 的 `init_task_convert_func`（约 346 行起）里，找到 `TS_TASK_TYPE_EVENT_RECORD` 和 `TS_TASK_TYPE_STREAM_WAIT_EVENT` 这两种任务类型对应的翻译函数（`convert_event_record_task` / `convert_stream_wait_event_task`），看它们翻译的是哪个 ID（提示：event id）。这两种任务类型正是后面 4.4 节 esched 事件的硬件侧对应物。
   - 在 `ts_agent_resource.c` 确认所有翻译最终都汇聚到 `hal_kernel_hvtsdrv_resid_v2p` 一个函数。
3. **需要观察的现象**：处理循环中出错不 break、拷贝用 `memcpy_s`、写回用 `hal_kernel_hvtsdrv_sq_write` + `hal_kernel_hvtsdrv_sq_irq_trigger`。
4. **预期结果**：你能画出「VSQ 槽 → memcpy → convert_task（按 type 翻译 v_id）→ sq_write → irq_trigger」的链路图。

#### 4.2.5 小练习与答案

- **练习 1**：为什么每个 VSQ 要用「单线程」工作队列，而不是共享一个全局队列？
  - **答**：单线程队列保证同一条 VSQ 的任务**串行**处理，避免并发翻译与回投导致物理 SQ 中的任务乱序（任务间往往有依赖）。不同 VSQ 之间彼此独立，各自一个队列可并行，吞吐与正确性兼得。
- **练习 2**：`proc_vsq` 末尾的 `retry_times` 机制解决了什么问题？
  - **答**：处理 `[head, tail)` 期间，虚拟机可能又往 VSQ 追加了新任务使 tail 前移。retry 一次能尽量在一次回调里把「处理期间新到」的任务也处理掉，减少 trsdrv 的回调次数。

---

### 4.3 event_sched 事件调度模型

#### 4.3.1 概念说明

esched（Event Schedule，事件调度）和 ts_agent 处于调度链路的「另一个面」。如果说 TS Agent 解决的是「任务怎么排队投递」，那么 esched 解决的是「**不同线程/CPU 之间怎么同步与传消息**」。

它用三个概念建模设备的并发同步：

- **组（group，grp）**：调度基本单位，绑定到某类 CPU（控制核 `GRP_TYPE_BIND_CP_CPU` 或数据核 `GRP_TYPE_BIND_DP_CPU`）。一个组里挂多个线程。
- **线程（thread，tid）**：组内的逻辑执行体。线程通过**事件位图（event_bitmap）** 订阅自己关心的事件。
- **事件（event）**：一个 `(event_id, subevent_id)` 二元组，可携带一段消息（msg）。提交（submit）事件相当于「发信号 + 带数据」，等待（wait）事件相当于「阻塞到收到信号」。

这非常像一套轻量的「发布—订阅 + 阻塞等待」原语，跑在设备侧的 AICPU/CCPU 上，由主机经 esched 接口驱动。它向上支撑了 stream/event 同步、跨进程消息（`EVENT_DRV_MSG` / `EVENT_DRV_MSG_EX`）等 Runtime 能力。

#### 4.3.2 核心流程

一个典型的「订阅 → 等待 → 提交 → 唤醒」往返：

```text
等待方（消费者）                          提交方（生产者）
─────────────────                       ─────────────────
halEschedAttachDevice(dev)               halEschedAttachDevice(dev)
halEschedCreateGrpEx(dev, grp_para, &gid)
halEschedSubscribeEvent(dev, gid, tid, bitmap)  // 订阅事件位图
halEschedWaitEvent(dev, gid, tid, timeout, &evt)
      └─ esched_wait_event_comm()
           ├─ esched_finish_call_back()   // 先回调上一次的完成函数
           ├─ esched_dev_ioctl(SCHED_WAIT_EVENT_ID)  // ioctl 阻塞
           └─ esched_save_wait_info()     // 保存本次等待，供下次回调
                                          halEschedSubmitEvent(dev, &event)
                                            └─ esched_submit_event_comm()
                                                 └─ esched_dev_ioctl(SCHED_SUBMIT_EVENT_ID)
   ←（内核唤醒，wait 返回，填好 event_info）  ←
```

所有 `halEsched*` 接口都不是直接操作硬件，而是统一经 `esched_dev_ioctl` 发 ioctl 给字符设备 `/dev/event_sched`，由内核里的 esched 模块真正完成调度。

#### 4.3.3 源码精读

统一的 ioctl 通道（含懒打开、fork 安全、错误重试）：

[src/ascend_hal/esched/comm/event_sched.c:596-625](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/esched/comm/event_sched.c#L596-L625) —— `esched_dev_ioctl` 是所有 esched 操作的总入口：`while(1)` 里先确保设备已 `esched_dev_init` 打开、取 fd、`esched_ioctl`（即 `ioctl`）。若返回 `DRV_ERROR_FILE_OPS` 说明 fd 因 fork 失效，调 `esched_init_global_fork` 清掉后重试；其他非零返回会顺手 `esched_share_log_read` 读一下设备日志辅助排障。

设备打开与 fd 数组管理：

[src/ascend_hal/esched/comm/event_sched.c:520-576](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/esched/comm/event_sched.c#L520-L576) —— `esched_dev_init` 用 `g_esched_init_mutex` 保护，fd 已开则直接返回（幂等），否则 `esched_open(SCHED_CHAR_DEV_FULL_NAME)` 打开 `/dev/event_sched` 并初始化调度 CPU 掩码。`sched_dev_fd[dev_id]` 是按设备号索引的 fd 数组（`ESCHED_DEV_NUM = 66`，即 64 逻辑设备 + 1 后备 + 1 主机虚拟设备）。

提交事件：

[src/ascend_hal/esched/comm/event_sched.c:834-873](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/esched/comm/event_sched.c#L834-L873) —— `esched_submit_event_comm` 把 `event_summary` 拆进 `sched_ioctl_para_submit`，盖好提交时间戳，`esched_dev_ioctl(SCHED_SUBMIT_EVENT_ID, ...)` 下发；`halEschedSubmitEvent` 是它的对外门面。

等待事件（含完成回调与线程局部等待记录）：

[src/ascend_hal/esched/comm/event_sched.c:653-697](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/esched/comm/event_sched.c#L653-L697) —— `esched_wait_event_comm` 先 `esched_finish_call_back` 回调上一次等待注册的完成函数（见下），再 `SCHED_WAIT_EVENT_ID` 阻塞（被 `DRV_ERROR_WAIT_INTERRUPT` 打断会自动重试），成功后 `esched_save_wait_info` 把本次事件存起来。

[src/ascend_hal/esched/comm/event_sched.c:714-728](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/esched/comm/event_sched.c#L714-L728) —— `halEschedWaitEvent` 是对外门面，组装 `esched_thread_info` 后委托给 `esched_wait_event_comm`。

> **关键设计：每线程的「等待信息」链表**。esched 用 `pthread_key`（线程局部存储，TLS）为每个线程维护一个等待信息链表，节点按 `(dev_id, grp_id, thread_id)` 唯一索引。`esched_save_wait_info` 在每次 wait 成功后更新节点；下一次 wait 开头的 `esched_finish_call_back` 就能据 `(dev,grp,tid)` 找到上次的节点、调用用户注册的 `esched_finish_func[grp][event_id]` 完成「事件到达」回调，再把节点置为无效。这样 Runtime 可以在「每次轮到某线程执行」时得到通知。

相关数据结构在头文件：

[src/ascend_hal/esched/comm/event_sched.h:80-108](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/esched/comm/event_sched.h#L80-L108) —— `esched_grp_info`（gid+type+name）、`esched_thread_info`（dev/gid/tid 三元组）、`esched_thread_wait_info`（链表节点，带 `event_valid` 与 `event_info`）。

创建组与订阅：

[src/ascend_hal/esched/comm/event_sched.c:1052-1078](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/esched/comm/event_sched.c#L1052-L1078) —— `esched_create_grp` 把组类型、线程数、组名序列化进 `sched_ioctl_para_add_grp` 下发 `SCHED_PROC_ADD_GRP_ID`，并在用户态 `sched_grp[]` 里记账。

[src/ascend_hal/esched/comm/event_sched.c:1021-1032](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/esched/comm/event_sched.c#L1021-L1032) —— `halEschedSubscribeEvent` 把事件位图下发给内核，让指定线程开始「关心」这些事件。

库加载时的初始化：

[src/ascend_hal/esched/comm/event_sched.c:1590-1600](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/esched/comm/event_sched.c#L1590-L1600) —— `__attribute__((constructor))` 的 `esched_init` 在动态库加载时把 fd 数组清零、初始化各组互斥锁、创建共享日志。与 HDC/URD 一样采用「constructor 懒初始化」套路。

#### 4.3.4 代码实践

1. **实践目标**：理解 esched 的「同步事件」（提交后等应答）这一最常用模式。
2. **操作步骤**：
   - 打开 `src/ascend_hal/esched/comm/event_sched_app.c`，阅读 `halEschedSubmitEventSync`（592 行起）。它做的是：`esched_alloc_event_res` 从资源池领一个空闲 `(gid,tid,event_id)` → `esched_fill_sync_msg` 把这个三元组填进消息 → `halEschedSubmitEvent` 提交 → `esched_wait_sync_event` 阻塞等应答 → 用 `submit_timestamp` 和 `subevent_id` 校验确实是本次的应答 → 释放资源。
   - 再看 `esched_res_init`（296 行起）：它一次性预创建多组（CP 核 4 组×若干事件类型、DP 核 1 组、驱动内部 1 组）并订阅事件，把这些「等待槽」池化起来。这正是 `esched_alloc_event_res` 能 O(1) 领到空闲槽的原因。
3. **需要观察的现象**：`halEschedSubmitEventSync` 内部其实是「submit + wait」两步；资源池用 `occupied_flag` 加锁管理空闲槽。
4. **预期结果**：你能解释「为什么 esched 能像 RPC 一样用：提交一个带 subevent_id 的事件，对面处理后回填同一 gid/event_id，这边就被唤醒」。

#### 4.3.5 小练习与答案

- **练习 1**：`esched_dev_ioctl` 为什么要在 `DRV_ERROR_FILE_OPS` 时清 fd 并重试？
  - **答**：进程 fork 后，子进程继承的 fd 对内核设备已失效（属于父进程的 attach）。检测到 `DRV_ERROR_FILE_OPS` 即重新打开设备、重建 attach，保证 fork 后子进程仍能正常 ioctl。
- **练习 2**：完成回调 `esched_finish_func` 为什么用 `[grp][event]` 的二维数组而不是链表？
  - **答**：grp 和 event 数量有限（`SCHED_MAX_GRP_NUM`、`EVENT_MAX_NUM`），二维数组可 O(1) 定位，比链表更快；这是用空间换时间的典型表驱动设计。

---

### 4.4 esched 适配层与内核通道：与 TRS、ts_agent 的协作

#### 4.4.1 概念说明

前两节分别讲了 ts_agent（任务面）和 event_sched（事件面）。这一节回答最关键的问题：**它们俩怎么协作？**

答案分两层：

1. **同一个数据面底座**：esched 提交的事件，并不是走一条独立的「事件总线」，而是被打包成 **topic SQE**，复用 u6-l3 讲过的 **TRS SQ/CQ 通道**送到设备。也就是说，任务（ts_agent 投递的）和事件（esched 提交的）在设备侧共用同一套 SQ/CQ 机制。
2. **ID 空间对齐**：ts_agent 在翻译 VSQ 任务时，对 `EVENT_RECORD` / `STREAM_WAIT_EVENT` 这类任务会翻译 `event_id`（`TSDRV_EVENT_SW_ID`）。这些 event id 必须与 esched 注册/订阅的事件空间对齐——否则虚拟机里「等待某个 event」就和「记录/通知那个 event」对不上号。这就是 `convert_event_id` 与 esched 共用一个 trsdrv 资源管理（`hal_kernel_hvtsdrv_resid_v2p`）的意义。

此外，esched 自身还有「适配层」（`esched_adapt.c`）负责 attach 引用计数、能力判定、时间基换算，以及一个「驱动内部事件处理线程」（`drv_event_proc.c`）做设备→主机的异步消息分发。

#### 4.4.2 核心流程

把 ts_agent + esched 放在一张图里看协作：

```text
            ┌─────────── 虚拟机 (VF) ───────────┐
            │  Runtime 写任务到 虚拟 SQ (VSQ)    │
            │  Runtime 用 halEsched* 操作事件    │
            └───────────────┬───────────────────┘
                            │ (VSQ 在 Host 内核可见)
   ┌────────────────────────┴───────────────────────────┐
   │                Host 内核态                          │
   │  trsdrv ──回调──> ts_agent                         │
   │     schedule_vsq_work → proc_vsq                   │
   │       convert_task:                                │
   │         v_stream_id  ─resid_v2p→ stream_id  ─┐     │
   │         v_event_id   ─resid_v2p→ event_id   ─┼─┐   │  (trsdrv 维护
   │         v_model_id   ─resid_v2p→ model_id   ─┘ │   │   VF→PF 的
   │       hal_kernel_hvtsdrv_sq_write → 物理 SQ     │   │   id 映射表)
   └────────────────────────────────┬─────────────────┘
                                    │
   ┌────────────────────────────────┴─────────────────┐
   │              Host 用户态 (libascend_hal.so)        │
   │  esched: halEschedSubmitEvent/WaitEvent           │
   │     └─ esched_dev_ioctl → /dev/event_sched        │
   │  topic 桥: esched_fill_topic_sqe                  │
   │     event_summary → topic_sched_sqe               │
   │     (topic_id=event_id, gid=grp_id)               │
   └────────────────────────────────┬─────────────────┘
                                    │
                            ┌───────┴────────┐
                            ▼                ▼
                      设备 TRS SQ/CQ     设备 AICPU/CCPU
                      (任务与事件共用)    (真正执行/调度)
```

要点：**ts_agent 与 esched 是「任务面」与「事件面」两条平行链路，但都汇聚到设备侧同一套 TRS SQ/CQ，并且 event id 由同一个 trsdrv 资源管理来分配与翻译，从而保证二者语义对齐。**

#### 4.4.3 源码精读

**（a）esched 适配层：attach 引用计数与能力判定**

[src/ascend_hal/esched/esched_adapt.c:72-96](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/esched/esched_adapt.c#L72-L96) —— `esched_attach_device_inner` 用 `attach_refcnt[dev_id]` 做引用计数：第一个 attach 才真正 `SCHED_ATTACH_PROCESS_TO_CHIP_ID` 把进程绑到芯片，后续 attach 只 `refcnt++`；detach 时 `refcnt--`，降到 0 才真正解绑。这避免同一进程多次打开设备时重复 attach，与 SVM 的双检锁/引用计数思想一致。

[src/ascend_hal/esched/esched_adapt.c:147-150](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/esched/esched_adapt.c#L147-L150) —— `esched_support_extern_interface` 等一组「能力判定」函数，让 `event_sched.c` 里的扩展接口（如 `halEschedSubmitEventBatch`、`halEschedThreadGiveup`）在不同平台/编译开关下优雅降级（返回 `DRV_ERROR_NOT_SUPPORT`），这是 u3-l5 讲过的「编译期宏 + 运行期判断」双层能力控制。

[src/ascend_hal/esched/esched_adapt.c:54-70](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/esched/esched_adapt.c#L54-L70) —— `sched_adapt_curr_time` 把用户态时间戳换算到内核时间基（`g_sched_usr_time`/`g_sched_kernel_time` 偏移），用于跨态 trace 对齐。

**（b）驱动内部事件处理线程：设备→主机异步消息**

[src/ascend_hal/esched/comm/drv_event_proc.c:261-309](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/esched/comm/drv_event_proc.c#L261-L309) —— `drv_event_thread_proc` 是一个常驻线程（`prctl` 改名为 `drv_event_proc`）：先 `drv_event_query_grid` 查出本进程的事件组 gid，然后 `while(1)` 里 `esched_wait_event_ex` 阻塞等 `EVENT_DRV_MSG`/`EVENT_DRV_MSG_EX`，收到后 `drv_event_proc` 按 `subevent_id` 分派给已注册的处理函数。这是「设备主动通知主机」的通道，例如 SVM 的 mmap/munmap 通知就经此上报。

[src/ascend_hal/esched/comm/drv_event_proc.c:64-67](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/esched/comm/drv_event_proc.c#L64-L67) —— `drv_registert_event_proc` 按 `subevent_id` 把处理函数登记进 `g_drv_event_proc[]`，是表驱动分派的注册入口。

**（c）topic 桥：esched 事件复用 TRS SQ/CQ（协作的关键证据）**

[src/ascend_hal/esched/esched_topic_sqe.c:33-61](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/esched/esched_topic_sqe.c#L33-L61) —— `esched_fill_sqcq_alloc_info` 在分配 TRS 通道时，把 `ext_msg->msg_header.type` 设为 `1`（即 `CHAN_SUB_TYPE_HW_TOPIC_SCHED`），并填好 SQE/CQE 尺寸（`TOPIC_SCHED_TASK_SQE_SIZE/CQE_SIZE`）。这说明 esched 的事件通道本质上是一条**特殊子类型的 TRS SQ/CQ**。

[src/ascend_hal/esched/esched_topic_sqe.c:73-117](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/esched/esched_topic_sqe.c#L73-L117) —— `esched_fill_topic_sqe` 把一个 `event_summary` 打包成 `topic_sched_sqe`：`topic_id = event_id`、`subtopic_id = subevent_id`、`gid = grp_id`，消息体拷进 `user_data`。也就是说，一个 esched 事件就是一条 topic SQE，经 TRS 数据面送到设备。

**（d）ts_agent 侧对 event id 的翻译（协作的另一端）**

[src/sdk_driver/ts_agent/src/ts_agent_vsq_proc.c:40-55](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/ts_agent/src/ts_agent_vsq_proc.c#L40-L55) —— `convert_event_record_task` 把 VSQ 任务里的 `v_event_id` 经 `convert_event_id`（底层 `TSDRV_EVENT_SW_ID` 的 `resid_v2p`）翻成物理 event id。这条物理 event id 与 esched 注册的事件处于同一空间，于是虚拟机里「记录事件」和主机/设备侧「等待事件」能正确握手。

[src/sdk_driver/inc/trs/tsdrv_interface.h:36-49](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/inc/trs/tsdrv_interface.h#L36-L49) —— `enum tsdrv_id_type` 列出所有可翻译的 id 种类（`TSDRV_STREAM_ID`/`TSDRV_EVENT_SW_ID`/`TSDRV_MODEL_ID`/`TSDRV_SQ_ID` 等），是 trsdrv 资源管理的「分类字典」，ts_agent 与 esched 共用它。

#### 4.4.4 代码实践

1. **实践目标**：用源码证据说明「ts_agent 代理任务下发、esched 分发事件，二者共用 TRS 数据面并对齐 event id」。
2. **操作步骤**：
   - **任务面**：从 `ts_agent_module.c` 的 `ts_agent_init`（注册 `tsagent_vsq_proc`）→ `ts_agent_vsq_worker.c` 的 `schedule_vsq_work`（投递到工作队列）→ `ts_agent_vsq_proc.c` 的 `proc_vsq_by_range`（逐条翻译 + `hal_kernel_hvtsdrv_sq_write` 回投物理 SQ）。在关键行旁批注「这一步把虚拟机的任务翻译后送进物理 SQ」。
   - **事件面**：从 `event_sched.c` 的 `halEschedSubmitEvent`（`esched_dev_ioctl(SCHED_SUBMIT_EVENT_ID)` 到 `/dev/event_sched`）→ `esched_topic_sqe.c` 的 `esched_fill_topic_sqe`（事件→topic SQE）。批注「事件最终也是一条 TRS SQE」。
   - **协作点**：在 `ts_agent_vsq_proc.c` 的 `convert_event_record_task` 与 `event_sched.c` 的 `halEschedCreateGrpEx`/`halEschedSubscribeEvent` 之间画一条连线，标注「二者共用 trsdrv 的 event 资源空间（`TSDRV_EVENT_SW_ID`）」。
3. **需要观察的现象**：任务面用内核工作队列异步处理、出错不中断；事件面用 ioctl 同步下发、用引用计数管理 attach；二者都指向 trsdrv 这同一资源管理者。
4. **预期结果**：你能口述清楚「为什么虚拟机里写一个 event_record 任务（ts_agent 翻译它）能让主机侧 esched wait 的线程被唤醒（同一 event id 空间 + 同一 TRS 数据面）」。

#### 4.4.5 小练习与答案

- **练习 1**：esched 的事件如果不用 TRS SQ/CQ，单独实现一套硬件通道，会有什么代价？
  - **答**：要新增一条独立的硬件队列与中断通路，增加固件复杂度和面积；且任务与事件无法在同一个调度器里统一排序、统一依赖管理。复用 TRS SQ/CQ 让「任务」和「事件」在设备侧共享一套调度基础设施，是典型的「一条数据面承载多种业务」设计。
- **练习 2**：`drv_event_thread_proc` 线程和 `halEschedSubmitEventSync` 的等待方，二者都在「等事件」，区别是什么？
  - **答**：`drv_event_thread_proc` 是**常驻服务线程**，被动等待设备主动上报的 `EVENT_DRV_MSG(_EX)`（设备→主机异步通知），收到后分派给注册的处理函数；`halEschedSubmitEventSync` 的等待方是**一次性请求—应答**，提交后等本次请求对应的应答事件（主机→设备→主机的一次 RPC 往返）。前者是「服务器」，后者是「客户端」。

---

## 5. 综合实践

**任务：绘制「TS Agent + esched 任务调度与事件同步协作图」，并用源码行号佐证每一个箭头。**

请按下列步骤完成：

1. **画两列**：左列「Host 内核态（ts_agent）」，右列「Host 用户态（esched）」，下方汇合到「设备 TRS SQ/CQ + AICPU/CCPU」。
2. **左列任务链路**（用本讲源码行号标注）：
   - `ts_agent_init` 注册 `tsagent_vsq_proc`（`ts_agent_module.c:63-67`）
   - trsdrv 回调 → `schedule_vsq_work` 入队（`ts_agent_vsq_worker.c:235`）
   - 工作线程 `proc_vsq_work` → `proc_vsq` 遍历 VSQ（`ts_agent_vsq_proc.c:551`）
   - `proc_vsq_by_range` 拷贝 + 翻译 + `hal_kernel_hvtsdrv_sq_write`（`ts_agent_vsq_proc.c:516-531`）
   - `convert_event_id` 经 `resid_v2p`（`ts_agent_resource.c:93-95`）
3. **右列事件链路**：
   - `halEschedSubmitEvent` → `esched_submit_event_comm`（`event_sched.c:863-873`）
   - `esched_dev_ioctl` → `/dev/event_sched`（`event_sched.c:596-625`）
   - `esched_fill_topic_sqe` 打包成 topic SQE（`esched_topic_sqe.c:73-117`）
4. **画出两个汇合点**：
   - 两条链路都指向「设备 TRS SQ/CQ」（topic 桥是证据）。
   - 两条链路在「event id 空间」上对齐（`TSDRV_EVENT_SW_ID`，`tsdrv_interface.h:40`）。
5. **写一段总结**（不少于 5 句）：说明在没有虚拟机时（裸机）这条链路如何退化（ts_agent 不参与，Runtime 直接写物理 SQ + 直接调 esched），在有虚拟机时 ts_agent 如何多承担一层「翻译」。

> 说明：本实践为「源码阅读型实践」，无需真实硬件即可完成；若需在真机上验证，可结合 u8-l1 的日志体系，在虚拟化场景下观察 `tsa_<dev>_<vf>_<ts>_<vsq>` 工作队列的内核日志与 `/dev/event_sched` 的设备日志。

---

## 6. 本讲小结

- **ts_agent** 是运行在 Host 内核态的「任务调度代理」，专为 SR-IOV 虚拟化场景设计：它通过注册回调给 trsdrv，在虚拟机的 VSQ 与物理 SQ 之间做翻译与投递。
- **vsq_worker** 采用「每条 VSQ 一个单线程工作队列」的设计，保证同一条 VSQ 串行处理、不同 VSQ 并行；`proc_vsq` 遍历 head→tail，逐条 `memcpy → convert_task → sq_write → irq_trigger`，且出错不中断以避免丢失 CQ 上报。
- **ID 翻译**是 ts_agent 的核心动作：`convert_stream/event/notify/model_id` 全部汇聚到 `hal_kernel_hvtsdrv_resid_v2p`，由 trsdrv 维护 VF→PF 的资源映射。
- **event_sched（esched）** 用「组—线程—事件」三要素 + submit/wait/ack/subscribe 提供设备侧并发同步原语，所有操作经统一通道 `esched_dev_ioctl` 下发到字符设备 `/dev/event_sched`，由内核 esched 模块实现。
- esched 用 `pthread_key` 维护每线程等待链表实现「完成回调」，用 `attach_refcnt` 引用计数管理进程与芯片的绑定，用 `event_sched_app.c` 提供「同步事件（类 RPC）」的高层封装。
- **二者协作**的关键是：esched 事件经 `esched_topic_sqe` 打包成 topic SQE，复用 u6-l3 的 **TRS SQ/CQ 数据面**；且 ts_agent 翻译 event id 与 esched 注册事件共用同一个 trsdrv 资源空间（`TSDRV_EVENT_SW_ID`），从而任务面与事件面语义对齐。

---

## 7. 下一步学习建议

- **u6-l5（FMS 故障管理）**：继续单元 6，进入故障检测与恢复（`soft_fault`、`os_reset`、`dms_sensor`），看设备发生异常时调度链路如何被中断与恢复。
- **u7-l1（RoCE / RDMA Lite）** 与 **u7-l5（vascend 算力切分）**：前者会把本讲的「数据面复用」思想推到 RDMA 场景；后者会展开本讲反复提到的「VF/虚拟化」全貌——ts_agent 正是为 vascend 切分出的 VF 服务的，读完会对 ts_agent 存在的意义有更立体的认识。
- **延伸阅读**：可对照 `src/sdk_driver/esched/`（内核侧 esched 实现）与 `src/sdk_driver/trsdrv/`（u6-l3）的源码，验证「事件与任务共用 TRS SQ/CQ」这一结论在内核侧是如何落地的。
