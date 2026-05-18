# ERD 테이블·컬럼 사전 v6.3

> Cloud SQL 실측 기준 (2026-04-14) | 12 테이블, 12 FK

---

## 1. 장소 (places) — 535,431 row

서울시 통합 장소 마스터. 18 카테고리, 48 source.

| # | 컬럼 | 한글명 | 타입 | PK | FK | NULL | 기본값 | 설명 |
|---|---|---|---|---|---|---|---|---|
| 1 | place_id | 장소ID | VARCHAR(36) | PK | | NO | | UUID 또는 소스별 자연키 |
| 2 | name | 상호명 | VARCHAR(200) | | | NO | | |
| 3 | category | 대분류 | VARCHAR(50) | | | NO | | v0.2 18종 (음식점/카페/주점 등) |
| 4 | sub_category | 소분류 | VARCHAR(100) | | | YES | | 업태/업종 세분류 |
| 5 | address | 주소 | TEXT | | | YES | | 도로명 또는 지번 |
| 6 | district | 자치구 | VARCHAR(50) | | | NO | | 비정규화 (25 서울 자치구) |
| 7 | geom | 좌표 | geometry(Point,4326) | | | YES | | PostGIS WGS84 좌표 |
| 8 | phone | 전화번호 | VARCHAR(20) | | | YES | | |
| 9 | google_place_id | 구글장소ID | VARCHAR(100) | | | YES | | deprecated |
| 10 | booking_url | 예약URL | TEXT | | | YES | | 딥링크 |
| 11 | raw_data | 원본데이터 | JSONB | | | YES | | CSV 전체 row 보존 |
| 12 | source | 데이터출처 | VARCHAR(50) | | | NO | | 48종 source 태그 |
| 13 | created_at | 생성일시 | TIMESTAMPTZ | | | YES | CURRENT_TIMESTAMP | |
| 14 | updated_at | 수정일시 | TIMESTAMPTZ | | | YES | CURRENT_TIMESTAMP | |
| 15 | is_deleted | 삭제여부 | BOOLEAN | | | NO | false | 소프트 삭제 |
| 16 | geog | 지리좌표 | geography | | | YES | | GENERATED STORED (거리 검색용) |

---

## 2. 행사 (events) — 7,301 row

축제·공연·전시 통합. 8 source.

| # | 컬럼 | 한글명 | 타입 | PK | FK | NULL | 기본값 | 설명 |
|---|---|---|---|---|---|---|---|---|
| 1 | event_id | 행사ID | VARCHAR(36) | PK | | NO | gen_random_uuid() | UUID |
| 2 | title | 행사명 | VARCHAR(200) | | | NO | | |
| 3 | category | 분류 | VARCHAR(50) | | | YES | | 공연/전시/축제 등 |
| 4 | place_name | 장소명 | TEXT | | | YES | | 비정규화 |
| 5 | address | 주소 | TEXT | | | YES | | 비정규화 |
| 6 | district | 자치구 | VARCHAR(50) | | | YES | | 비정규화 |
| 7 | geom | 좌표 | geometry(Point,4326) | | | YES | | |
| 8 | date_start | 시작일 | DATE | | | YES | | |
| 9 | date_end | 종료일 | DATE | | | YES | | |
| 10 | price | 가격정보 | TEXT | | | YES | | 무료/유료/금액 |
| 11 | poster_url | 포스터URL | TEXT | | | YES | | 이미지 |
| 12 | detail_url | 상세URL | TEXT | | | YES | | 외부 페이지 |
| 13 | summary | 요약 | TEXT | | | YES | | |
| 14 | source | 데이터출처 | VARCHAR(50) | | | YES | | |
| 15 | raw_data | 원본데이터 | JSONB | | | YES | | |
| 16 | created_at | 생성일시 | TIMESTAMPTZ | | | YES | now() | |
| 17 | updated_at | 수정일시 | TIMESTAMPTZ | | | NO | now() | |
| 18 | is_deleted | 삭제여부 | BOOLEAN | | | NO | false | |

---

## 3. 행정동 (administrative_districts) — 427 row

서울 행정동 경계 마스터. 자연키 PK.

| # | 컬럼 | 한글명 | 타입 | PK | FK | NULL | 기본값 | 설명 |
|---|---|---|---|---|---|---|---|---|
| 1 | adm_dong_code | 행정동코드 | VARCHAR(20) | PK | | NO | | 8자리 자연키 |
| 2 | adm_dong_name | 행정동명 | VARCHAR(50) | | | NO | | |
| 3 | district | 자치구 | VARCHAR(50) | | | NO | | 25 서울 자치구 |
| 4 | geom | 경계 | geometry(MultiPolygon,4326) | | | YES | | PostGIS 폴리곤 |
| 5 | created_at | 생성일시 | TIMESTAMPTZ | | | NO | now() | |
| 6 | updated_at | 수정일시 | TIMESTAMPTZ | | | NO | now() | |

---

## 4. 생활인구 (population_stats) — 278,880 row

행정동별 시간대별 인구. append-only.

| # | 컬럼 | 한글명 | 타입 | PK | FK | NULL | 기본값 | 설명 |
|---|---|---|---|---|---|---|---|---|
| 1 | id | 생활인구ID | BIGSERIAL | PK | | NO | auto | |
| 2 | base_date | 기준일 | DATE | | | NO | | |
| 3 | time_slot | 시간대 | SMALLINT | | | NO | | 0~23 |
| 4 | adm_dong_code | 행정동코드 | VARCHAR(20) | | FK | NO | | → administrative_districts ON DELETE RESTRICT |
| 5 | total_pop | 총인구수 | INT | | | NO | 0 | |
| 6 | raw_data | 원본데이터 | JSONB | | | YES | | 32 컬럼 전량 보존 |
| 7 | created_at | 생성일시 | TIMESTAMPTZ | | | NO | now() | |

---

## 5. 행정동코드매핑 (admin_code_aliases) — 11 row

행정동 구→신 코드 변환 테이블.

| # | 컬럼 | 한글명 | 타입 | PK | FK | NULL | 기본값 | 설명 |
|---|---|---|---|---|---|---|---|---|
| 1 | id | 매핑ID | BIGSERIAL | PK | | NO | auto | |
| 2 | old_code | 구코드 | VARCHAR(20) | | | NO | | UNIQUE(old_code, new_code) |
| 3 | new_code | 신코드 | VARCHAR(20) | | FK | NO | | → administrative_districts ON DELETE RESTRICT |
| 4 | change_type | 변경유형 | VARCHAR(20) | | | NO | | rename/split/merge/new |
| 5 | change_note | 변경사유 | TEXT | | | YES | | |
| 6 | confidence | 신뢰도 | VARCHAR(10) | | | NO | | authoritative/high/medium/low |
| 7 | created_at | 생성일시 | TIMESTAMPTZ | | | NO | now() | |

---

## 6. 사용자 (users) — 0 row

이메일 + Google OAuth 이중인증.

| # | 컬럼 | 한글명 | 타입 | PK | FK | NULL | 기본값 | 설명 |
|---|---|---|---|---|---|---|---|---|
| 1 | user_id | 사용자ID | BIGSERIAL | PK | | NO | auto | |
| 2 | email | 이메일 | VARCHAR(200) | | | NO | | UNIQUE |
| 3 | password_hash | 비밀번호해시 | VARCHAR(200) | | | YES | | bcrypt (email 가입 시 필수) |
| 4 | auth_provider | 인증방식 | VARCHAR(20) | | | NO | 'email' | email \| google |
| 5 | google_id | 구글ID | VARCHAR(100) | | | YES | | google 가입 시 필수 |
| 6 | nickname | 닉네임 | VARCHAR(100) | | | YES | | |
| 7 | created_at | 생성일시 | TIMESTAMPTZ | | | NO | now() | |
| 8 | updated_at | 수정일시 | TIMESTAMPTZ | | | NO | now() | |
| 9 | is_deleted | 삭제여부 | BOOLEAN | | | NO | false | |

---

## 7. OAuth토큰 (user_oauth_tokens) — 0 row

Google OAuth refresh/access token 저장.

| # | 컬럼 | 한글명 | 타입 | PK | FK | NULL | 기본값 | 설명 |
|---|---|---|---|---|---|---|---|---|
| 1 | token_id | 토큰ID | BIGSERIAL | PK | | NO | auto | |
| 2 | user_id | 사용자ID | BIGINT | | FK | NO | | → users ON DELETE CASCADE |
| 3 | provider | 제공자 | VARCHAR(20) | | | NO | | google 등 |
| 4 | scope | 범위 | VARCHAR(100) | | | NO | | OAuth scope |
| 5 | refresh_token | 리프레시토큰 | VARCHAR(512) | | | NO | | |
| 6 | access_token | 액세스토큰 | VARCHAR(512) | | | YES | | |
| 7 | expires_at | 만료일시 | TIMESTAMPTZ | | | YES | | |
| 8 | created_at | 생성일시 | TIMESTAMPTZ | | | NO | now() | |
| 9 | updated_at | 수정일시 | TIMESTAMPTZ | | | NO | now() | |
| 10 | is_deleted | 삭제여부 | BOOLEAN | | | NO | false | |

---

## 8. 대화 (conversations) — 0 row

채팅 세션 메타. LangGraph thread_id 연동.

| # | 컬럼 | 한글명 | 타입 | PK | FK | NULL | 기본값 | 설명 |
|---|---|---|---|---|---|---|---|---|
| 1 | conversation_id | 대화ID | BIGSERIAL | PK | | NO | auto | |
| 2 | thread_id | 스레드ID | VARCHAR(100) | | | NO | | UNIQUE, LangGraph 연동 |
| 3 | user_id | 사용자ID | BIGINT | | FK | NO | | → users ON DELETE CASCADE |
| 4 | title | 제목 | VARCHAR(200) | | | YES | | 자동생성/수정 가능 |
| 5 | created_at | 생성일시 | TIMESTAMPTZ | | | NO | now() | |
| 6 | updated_at | 수정일시 | TIMESTAMPTZ | | | NO | now() | |
| 7 | is_deleted | 삭제여부 | BOOLEAN | | | NO | false | |

---

## 9. 메시지 (messages) — 0 row

대화 원본 영속 저장. **append-only** (UPDATE/DELETE 금지).

| # | 컬럼 | 한글명 | 타입 | PK | FK | NULL | 기본값 | 설명 |
|---|---|---|---|---|---|---|---|---|
| 1 | message_id | 메시지ID | BIGSERIAL | PK | | NO | auto | |
| 2 | thread_id | 스레드ID | VARCHAR(100) | | FK | NO | | → conversations.thread_id ON DELETE CASCADE |
| 3 | role | 역할 | VARCHAR(20) | | | NO | | user \| assistant \| system |
| 4 | blocks | 블록 | JSONB | | | NO | | 16종 WS 콘텐츠 블록 |
| 5 | created_at | 생성일시 | TIMESTAMPTZ | | | NO | now() | |

---

## 10. 북마크 (bookmarks) — 0 row

대화 위치 저장. 5종 프리셋 핀. Phase 2.

| # | 컬럼 | 한글명 | 타입 | PK | FK | NULL | 기본값 | 설명 |
|---|---|---|---|---|---|---|---|---|
| 1 | bookmark_id | 북마크ID | BIGSERIAL | PK | | NO | auto | |
| 2 | user_id | 사용자ID | BIGINT | | FK | NO | | → users ON DELETE CASCADE |
| 3 | thread_id | 스레드ID | VARCHAR(100) | | | NO | | |
| 4 | message_id | 메시지ID | BIGINT | | FK | NO | | → messages ON DELETE CASCADE |
| 5 | pin_type | 핀유형 | VARCHAR(20) | | | NO | | place/event/course/analysis/general |
| 6 | preview_text | 미리보기 | TEXT | | | YES | | 스니펫 |
| 7 | created_at | 생성일시 | TIMESTAMPTZ | | | NO | now() | |
| 8 | updated_at | 수정일시 | TIMESTAMPTZ | | | NO | now() | |
| 9 | is_deleted | 삭제여부 | BOOLEAN | | | NO | false | 소프트 삭제 |

---

## 11. 장소 북마크 (place_bookmarks) — 0 row

장소 단독 북마크. 대화 북마크(bookmarks)와 별도 테이블. 시점 스냅샷 보관. Phase 2.

| # | 컬럼 | 한글명 | 타입 | PK | FK | NULL | 기본값 | 설명 |
|---|---|---|---|---|---|---|---|---|
| 1 | bookmark_id | 북마크ID | BIGSERIAL | PK | | NO | auto | |
| 2 | user_id | 사용자ID | BIGINT | | FK | NO | | → users ON DELETE CASCADE |
| 3 | place_id | 장소ID | VARCHAR(100) | | | NO | | UUID 또는 gp_{google_place_id} |
| 4 | name | 장소명 | VARCHAR(200) | | | NO | | 시점 스냅샷 |
| 5 | category | 카테고리 | VARCHAR(50) | | | YES | | 시점 스냅샷 |
| 6 | address | 주소 | TEXT | | | YES | | 시점 스냅샷 |
| 7 | district | 자치구 | VARCHAR(50) | | | YES | | 시점 스냅샷 |
| 8 | lat | 위도 | DOUBLE PRECISION | | | YES | | 시점 스냅샷 |
| 9 | lng | 경도 | DOUBLE PRECISION | | | YES | | 시점 스냅샷 |
| 10 | rating | 평점 | REAL | | | YES | | 시점 스냅샷 |
| 11 | image_url | 이미지URL | TEXT | | | YES | | |
| 12 | summary | 요약 | TEXT | | | YES | | 최대 500자 |
| 13 | source_thread_id | 출처스레드ID | VARCHAR(100) | | | YES | | 발견 대화 추적 |
| 14 | source_message_id | 출처메시지ID | BIGINT | | | YES | | 발견 메시지 추적 |
| 15 | is_deleted | 삭제여부 | BOOLEAN | | | NO | false | 소프트 삭제 |
| 16 | created_at | 생성일시 | TIMESTAMPTZ | | | NO | now() | |
| 17 | deleted_at | 삭제일시 | TIMESTAMPTZ | | | YES | | 소프트 삭제 시각 |

UNIQUE (user_id, place_id) — 중복 시 idempotent 200 반환.

---

## 12. 공유링크 (shared_links) — 0 row

대화 공유 토큰. Phase 2.

| # | 컬럼 | 한글명 | 타입 | PK | FK | NULL | 기본값 | 설명 |
|---|---|---|---|---|---|---|---|---|
| 1 | share_id | 공유ID | BIGSERIAL | PK | | NO | auto | |
| 2 | share_token | 공유토큰 | VARCHAR(100) | | | NO | | UNIQUE |
| 3 | thread_id | 스레드ID | VARCHAR(100) | | | NO | | |
| 4 | user_id | 사용자ID | BIGINT | | FK | NO | | → users ON DELETE CASCADE |
| 5 | from_message_id | 시작메시지ID | BIGINT | | | YES | | 범위 공유 시 |
| 6 | to_message_id | 종료메시지ID | BIGINT | | | YES | | |
| 7 | expires_at | 만료일시 | TIMESTAMPTZ | | | YES | | |
| 8 | created_at | 생성일시 | TIMESTAMPTZ | | | NO | now() | |
| 9 | updated_at | 수정일시 | TIMESTAMPTZ | | | NO | now() | |
| 10 | is_deleted | 삭제여부 | BOOLEAN | | | NO | false | |

---

## 12. 피드백 (feedback) — 0 row

AI 응답 평가. **append-only** (updated_at/is_deleted 없음). Phase 3.

| # | 컬럼 | 한글명 | 타입 | PK | FK | NULL | 기본값 | 설명 |
|---|---|---|---|---|---|---|---|---|
| 1 | feedback_id | 피드백ID | BIGSERIAL | PK | | NO | auto | |
| 2 | user_id | 사용자ID | BIGINT | | FK | NO | | → users ON DELETE CASCADE |
| 3 | thread_id | 스레드ID | VARCHAR(100) | | | NO | | |
| 4 | message_id | 메시지ID | BIGINT | | FK | NO | | → messages ON DELETE CASCADE |
| 5 | rating | 평가 | VARCHAR(10) | | | NO | | up \| down |
| 6 | comment | 코멘트 | TEXT | | | YES | | 선택적 텍스트 |
| 7 | created_at | 생성일시 | TIMESTAMPTZ | | | NO | now() | |

---

## FK 관계 (13개)

| FROM | FROM 컬럼 | TO | TO 컬럼 | ON DELETE |
|---|---|---|---|---|
| population_stats | adm_dong_code | administrative_districts | adm_dong_code | RESTRICT |
| admin_code_aliases | new_code | administrative_districts | adm_dong_code | RESTRICT |
| user_oauth_tokens | user_id | users | user_id | CASCADE |
| conversations | user_id | users | user_id | CASCADE |
| messages | thread_id | conversations | thread_id | CASCADE |
| bookmarks | user_id | users | user_id | CASCADE |
| bookmarks | message_id | messages | message_id | CASCADE |
| place_bookmarks | user_id | users | user_id | CASCADE |
| shared_links | user_id | users | user_id | CASCADE |
| shared_links | from_message_id | messages | message_id | CASCADE |
| shared_links | to_message_id | messages | message_id | CASCADE |
| feedback | user_id | users | user_id | CASCADE |
| feedback | message_id | messages | message_id | CASCADE |
