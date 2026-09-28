# 07.2 部署、配置、恢复与告警

## 1. 拓扑和资源

单宿主Docker Compose：web/nginx、api、publisher、sweeper、worker-general、worker-inference、ai-inference、mysql、redis、minio。publisher/sweeper为worker镜像不同启动命令，不与API进程绑死。外部仅HTTPS443；API8000、AI8001、MySQL3306、Redis6379、MinIO9000仅内部网络；管理console不得公开。

建议试验宿主8逻辑CPU、16GiB内存、500GiB磁盘；这是起步配置非性能保证。初始内存上限api1GiB/general1GiB/inference1GiB/AI6GiB/MySQL3GiB/Redis512MiB/MinIO1GiB，余量留给OS。CUDA显存需求取真实bundle评测，未测不得承诺支持任意GPU；CPU profile并发1。worker-general并发2、worker-inference并发1；任务路由分别命名q.general/q.inference.cpu/q.inference.cuda。

### D-FINE-N CUDA部署补充

本期生产建议配置为D-FINE-N/ORT CUDA FP32，OCR仍为PP-OCRv4 CPU。API、数据库、Redis、Worker和规则无需GPU；只有ai-inference获得单GPU访问。首期不增加推理平台、跨主机调度或多模型动态装卸。CPU功能profile仍可开发/评测，CPU生产必须单独批准，不自动继承CUDA性能报告。

宿主使用Linux x86_64、受支持的NVIDIA驱动和NVIDIA Container Toolkit；驱动须满足实际锁定CUDA/cuDNN镜像要求。I-ML-01记录具体版本及验证命令结果，不从“有GPU”推定可运行。AI镜像只安装一种ORT发行包；CUDA、CPU分别构建并固定digest。GPU型号、总/空闲显存、模型稳定占用和峰值、宿主RAM及驱动写入评测快照。现有AI6GiB是宿主内存起步限制，不是显存保证；实测超限则有记录地调整，不隐瞒OOM。

未来compose.cuda.yaml仅覆盖既有ai-inference服务；AI_GPU_DEVICE_ID由运维选择宿主设备ID，容器中仅暴露这一张卡，ORT的逻辑device_id=0。此片段是待实现部署规范，不是仓库已有可运行Compose：

~~~yaml
services:
  ai-inference:
    environment:
      AI_DEVICE: cuda
      OMP_NUM_THREADS: "1"
      MKL_NUM_THREADS: "1"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              device_ids: ["${AI_GPU_DEVICE_ID:?select one host GPU}"]
              capabilities: [gpu]
~~~

device_ids与count互斥，不设置all；API/Worker不挂GPU，也不使用privileged代替设备授权。GPU设备预留不是显存配额或独占保证：部署前检查该卡现有工作负载，未获容量许可不得放置第二个模型实例。断网制品、只读目录、secret和内部网络规则保持不变。官方语法来源：https://docs.docker.com/compose/how-tos/gpu-support/。

启动顺序：宿主/容器设备可见性→锁文件/制品核验→spawn子进程→创建CUDA EP会话(use_tf32=0)→真实模型smoke及provider节点核对→加载CPU OCR并做smoke→/ready。各阶段共享120秒启动期限；任一步失败not_ready，不能暴露未校验模型。首次原型的辅助CPU节点先审核并固化进runtime lock，再打包/激活；新节点或整图回落CPU拒绝就绪。节点profile文件写受限临时目录，不含图像/文字，不写模型只读目录。

AI_DEVICE、路由device_profile、manifest.runtime_profile/runtime设备和/version必须一致；创建activation显式提交cuda，不能依赖DDL现有cpu默认值。历史run仍固定原设备，不回写改成cuda。/version回显真实adapter/profile/lock hash及检测/OCR设备；/health只表示supervisor存活。

每次OOM记录MODEL_OOM，回收子进程、关闭IPC、重载和smoke后才恢复ready；不使用仅empty_cache继续复用故障上下文的处理。GPU丢失按加载失败/未就绪处理，不静默改走CPU队列。一次请求初始并发1、多图顺序处理；OCR负载过重先测ocr_ms与标签数，不因此偷偷更换OCR模型或改GPU化。

容量验收记录10次预热和200case规定负载，并补充连续1000次合法请求（覆盖空目标、少标签、接近100目标边界）、10轮子进程终止/重载：无错误复用、无僵尸子进程、显存能够回到已测稳定区间。稳定区间以该环境10次预热后、每秒一次的20次空闲采样最大占用加256MiB为复核上界；超过判需调查/不通过，不能降低采样频率掩盖持续增长。此为项目工程门禁，不是已取得成绩。

单卡无双份显存余量时，停止领取新CUDA任务→排空在途任务→not_ready→卸载旧子进程→加载新bundle/lock→smoke→激活新route。新版本失败则恢复旧镜像/bundle/route并smoke，不改旧run。记录GPU已用/峰值显存、显存余量、重载耗时、CUDA EP失败次数及现有分阶段/队列指标；高基数GPU标识留环境快照，不按run生成监控标签。

## 2. 配置表

| 变量/secret | 默认/要求 | 使用者 |
|---|---|---|
| APP_ENV | 必填dev/test/production；进程间必须一致，不接受请求覆盖 | api/worker/AI/web构建配置 |
| AI_MODE | 必填real/mock；production禁止mock | AI |
| APPROVED_RELEASES_FILE | production必填只读台账，API不可写；匹配manifest/policy/report/tenant | api/worker/AI |
| DATABASE_URL_FILE | secret文件，禁止明文提交Git | api/worker/publisher/sweeper |
| REDIS_URL_FILE | 独立ACL账户，非空 | api/worker/publisher |
| S3_ENDPOINT / S3_BUCKET | 内网地址 / labsafe-private | api/worker/AI |
| S3_PUBLIC_ENDPOINT / S3_REGION | 前者必须等于PUBLIC_ORIGIN且无路径；后者us-east-1；签名path-style | API签名client；实际对象操作仍使用内网client |
| S3_ACCESS_KEY_FILE / S3_SECRET_KEY_FILE | 每服务最小权限、不同凭据 | 对象客户端 |
| S3_CLEANUP_ACCESS_KEY_FILE / S3_CLEANUP_SECRET_KEY_FILE | 仅general挂载，精确版本删除专用；普通处理器不取用 | object_cleanup |
| AI_ROUTES_FILE | 受控JSON；按tenant/model/dictionary/device映射内网endpoint/token_file | api/inference worker |
| AI_TOKEN_FILE | ≥32字节随机secret，非空 | worker/AI健康探针 |
| AI_MANIFEST_PATH / AI_DEVICE | 只读本地manifest / cpu或cuda | AI |
| AI_GPU_DEVICE_ID | CUDA Compose覆盖文件必填，恰一张宿主GPU；不进入用户请求 | 部署方/Compose |
| AI_MAX_INFLIGHT | 固定1，改变需重新容量评测 | AI |
| CSRF_KEY_FILE / SESSION_COOKIE_SECURE | secret / true | api |
| PUBLIC_ORIGIN | 实际HTTPS域，禁止通配 | api/web |
| TENANT_TIMEZONE | bootstrap必填，如Asia/Shanghai，存tenant表 | 初始化 |
| IMAGE_MAX_BYTES / IMAGE_MAX_PIXELS | 15728640 / 40000000 | api/worker |
| LOG_LEVEL | INFO，生产禁payload DEBUG | 所有服务 |
| OTEL_EXPORTER_OTLP_ENDPOINT | 内网收集器，离线时本地记录 | api/worker/AI |

镜像tag固定Git SHA且记录digest；实际依赖锁文件、CUDA/driver兼容矩阵和模型artifact digest必须在实施时实测。当前只有设计脚本依赖锁定，不能称全应用环境已可复现。

## 3. 启动与验证

### 3.1 IRR-05浏览器对象入口

选择同源HTTPS路径转发，保持单一443入口；不是新服务。PUBLIC_ORIGIN/S3_PUBLIC_ENDPOINT示例为https://labsafe.example.org（部署替换，非真实地址）。API使用一个内部client做HEAD/Get/Copy，另一个仅本地签名的client使用公共endpoint；不需从容器实际连公共域名才能签名。bucket固定labsafe-private，region两侧均us-east-1。

以下是未来nginx HTTPS server中的配置要求，非仓库已有运行栈；上层server只接受PUBLIC_ORIGIN的Host，未知Host拒绝。对象路径无rewrite，不使用带尾部URI的proxy_pass，不改变SigV4的host/path/query：

~~~nginx
location ^~ /labsafe-private/ {
    limit_except GET HEAD PUT { deny all; }
    client_max_body_size 15m;
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header Connection "";
    proxy_set_header Cookie "";
    proxy_set_header Authorization "";
    proxy_pass http://minio:9000;
    proxy_request_buffering off;
    proxy_buffering off;
    proxy_cache off;
    proxy_hide_header Set-Cookie;
    add_header Referrer-Policy no-referrer always;
    add_header Cache-Control "no-store" always;
    access_log off;
}
~~~

此公开路径仅服务预签名query认证，不接受浏览器AWS Authorization头；MinIO仍校验签名和IAM，不因代理可达就匿名可读。API/应用路径另行路由且不放宽其JSON上限。关闭对象路径URL访问日志，另用无query的状态/耗时指标；全站也设置Referrer-Policy:no-referrer。只同源，不添加Access-Control-Allow-Origin:*。PUT最多15MiB，其他方法403；不把MinIO XML错误透传当业务API JSON，前端按传输状态处理。

请求样例（query由SDK生成，禁止将占位符当实际签名）：

~~~text
PUT https://labsafe.example.org/labsafe-private/staging/{tenant}/{upload}?X-Amz-Algorithm=AWS4-HMAC-SHA256&...&X-Amz-Signature={sdk生成值}
Content-Type: image/png
Body: 原始PNG Blob；不发送FormData，不改签名URL

GET https://labsafe.example.org/labsafe-private/tenant/{tenant}/lab/{lab}/analysis/{image}/{sha}.png?versionId={已固定版本}&X-Amz-Algorithm=AWS4-HMAC-SHA256&...&X-Amz-Signature={sdk生成值}
~~~

UP-03/SEC-03实际验收：成功PUT→complete→analysis GET；越权用户不能取grant（404/403）；过期/篡改路径、query、Content-Type签名由MinIO拒绝403；超15MiB由nginx拒绝413；PUT新版本后原有GET仍读固定version；未签名请求拒绝（403或防存在性泄露的404）；无ListBucket能力；内部9000不从宿主对外映射。API启动发现公共端点不等于origin、带路径或非HTTPS则配置错误/not_ready，不退回内网URL。

实施时核验的官方参考；MinIO兼容行为须在实际锁定版本实测，不由文档测试代替：

~~~text
https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass
https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_set_header
https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html
https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-with-s3-policy-actions.html
~~~

### 3.2 服务启动顺序

1. 预加载镜像/制品/依赖离线包，校验SHA与许可；创建独立服务账户、私有bucket、版本化及TLS域。
2. 启动mysql/minio/redis，检查磁盘/连接/时钟UTC；运行Alembic升级与schema introspection，禁止API自动建表。
3. 使用受审计CLI bootstrap-tenant --code --name --timezone，密码从stdin交互输入；seed固定roles；紧急 reset-password --tenant --user --reason 撤销所有session，必须审计，不传密码命令行。
4. API启动，配置组织/模板；production导入已批准词典/模型/规则，挂载匹配批准台账再创建bundle/activation。dev/test可使用development fixture和synthetic规则，必须独立数据库/对象前缀、显式环境标识，不要求先有真实权重。
5. AI加载目标manifest，ready200及smoke通过后启动inference worker；启动publisher/sweeper/general/web。
6. 运行上传→推理→复核→整改→复查→导出最小闭环，核对版本/审计，无测试数据冒充真实安全样本。

API /health（内部无敏感数据）仅进程存活；/ready检查DB读、schema版本、Redis和对象服务可达。AI健康见专文。探针10秒一次，连续3次失败unhealthy；readiness失败移出接流量但不无限重启掩盖故障。业务后台等待AI恢复时不得让HTTP请求一直阻塞。

## 4. 发布和回滚

先备份和标记版本→停止接新写/领取→排空最长260秒→迁移→新版本smoke→恢复。模型蓝绿实例须有额外资源，否则停接任务、排空、替换manifest重启；activate只选已ready匹配bundle的实例，新旧run不能混用。

代码回滚到已验证digest；DB迁移优先前向修复，禁止自动降级丢列。模型/规则回滚只更新activation指针，旧run保留原版本。退役制品至少保留到所有引用证据保留期结束，不能删掉导致历史不可重放。

## 5. 告警与关闭条件

| 信号 | 触发 / 等级 | 动作及关闭 |
|---|---|---|
| 审计写失败、越租户结果、hash异常 | 任一次 / P1 | 停相关写/接纳；15分钟响应；查明且回归通过后人工关闭 |
| DB不可用、ready全部失败 | 连续1分钟 / P1 | 15分钟响应，恢复依赖；连续5分钟ready且任务收敛才关闭 |
| 磁盘使用 | ≥80% 10分钟P2，≥90% 1分钟P1 | 停非必要导出、扩容；低于75% 15分钟关闭，不能删引用证据 |
| 到期ready任务最老年龄 | >60秒5分钟 / P2 | 查publisher/sweeper/Redis；连续10分钟<30秒关闭 |
| dead_letter增长 | 15分钟新增≥1 / P2 | 逐项定位，修复后受审计replay；无新增30分钟且处理存量 |
| AI失败率/加载 | 5分钟≥10%且请求≥20，或连续3次加载失败 / P2 | 回滚已验证bundle；连续15分钟<1%且ready关闭 |
| CUDA端到端P95 | >30秒15分钟且样本≥100 / P2 | 查队列/资源，降并发或扩容；连续30分钟达标关闭 |
| 备份年龄 | >26小时 / P1 | 立即备份和验证；完成成功备份后关闭 |

指标含queue_age、outbox_oldest、lease_expired、fencing_rejections、retry/deadletter、stage_latency、AI_ready/OOM、audit_failure、object_hash_mismatch；低基数标签profile/stage/error，不按image_id/user_id打指标。日志可包含run/attempt/task/trace关联但不含图、OCR全文或签名URL。

## 6. 备份与恢复

工程目标RPO≤24小时、RTO≤4小时，尚须实测；每天02:00租户部署时区执行。备份前短暂暂停业务写/任务提交和清理，排空事务，取得MySQL一致性快照及所引用对象version清单；将DB dump、对象指定版本、模型/词典/规则manifest、配置版本一起归档并校验hash；secret独立加密托管。不能只备DB漏MinIO，不能把Redis当备份源。

恢复到隔离空环境：校验备份hash→恢复DB→恢复准确对象版本和制品→应用schema版本核验→清空恢复环境Redis→将遗留leased任务围栏失效并按sweeper收敛→重建pending调度→做跨租户、下载、推理/复核/整改/导出smoke→核对抽样20条证据和全部缺失对象扫描→记录实际RPO/RTO→双人确认后切流量。禁止向正在运行的生产库直接试恢复。

## 7. 断公网验收

预载全部制品后阻断外网出口但保留内网；运行闭环和故障重试，抓取连接日志证明无外部DNS/模型下载/遥测依赖。检查字体、前端assets、PDF渲染均本地。恢复网络后不得自动上传图片或日志。保留时间、配置、样本ID、请求trace与报告，不以“能打开页面”作为通过。
