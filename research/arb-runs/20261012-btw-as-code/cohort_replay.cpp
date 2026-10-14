// Offline multi-date activity-gated L1 replay (002/003 exact fixed-point scales). No network or exchange APIs.
// Fixed policy: entry_deterioration TP5/SL20/hard100; gateoff/q10/q25 only.
// CLI: --tape BTWTP002|BTWTP003 --activity BTWACT01 --thresholds CSV --output NEWDIR [--entry-start-ns NS]
// CSV: name,countA,countB,turnoverAunits,turnoverBunits,maxAgeNs; q10/q25 rows.
// BTWACT01: <qIIqqqqQ> = ns,counts[2],turnover1e8[2],ages_ns[2],warm_mask.
// Gates apply ONLY to new entries; all held-position risk/execution stays unchanged.
// Every scenario writes entry_budgets.jsonl before either entry consumes liquidity.
// Input: BTWTP002 (1e-5) or BTWTP003 (1e-6), then little-endian <qBBBBI8q> / <B7xqq> records.
// Header L1 prices use the magic-selected scale; update keys use exact 1e-8 price units.
// Money is exact 10^-(price_decimals+4) USDT; displayed-liquidity budgets are quarter-coins.
#include <algorithm>
#include <array>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

using I = int64_t;
using Wide = __int128_t;
namespace fs = std::filesystem;
I INITIAL = 1000000000000LL;
I MIN_NOTIONAL_TICKS = 500000;
I MAX_NOTIONAL_TICKS = 25000000;
int PRICE_DECIMALS=5,MONEY_DECIMALS=9;
I DEPTH_KEY_MULTIPLIER=1000;
std::array<I,2> NATIVE_TICK_UNITS{1,1};
std::string TAPE_FORMAT="BTWTP002";
const char* VENUE[2] = {"bybit", "bitget"};

static I checked(Wide x) {
    if (x < std::numeric_limits<I>::min() || x > std::numeric_limits<I>::max())
        throw std::runtime_error("integer overflow");
    return I(x);
}
static std::string dec(I x, int places) {
    bool neg=x<0; uint64_t n=neg ? uint64_t(-(x+1))+1 : uint64_t(x), base=1;
    for(int i=0;i<places;++i) base*=10;
    std::string f=std::to_string(n%base);
    return (neg?"-":"")+std::to_string(n/base)+"."+std::string(places-f.size(),'0')+f;
}
static I floor10(I q) { return std::max<I>(0,q)/10*10; }
static I ceildiv(I a,I b) { return a/b+(a%b!=0); }
static I fee(I p,I q) { return checked(Wide(3)*p*q); }
static I threshold(I p,I q,int bps) { return checked(Wide(p)*q*bps); }
static I read_i64(const unsigned char* p) {
    uint64_t n=0; for(int i=0;i<8;++i)n|=uint64_t(p[i])<<(i*8); return I(n);
}
static uint32_t read_u32(const unsigned char* p) {
    uint32_t n=0;for(int i=0;i<4;++i)n|=uint32_t(p[i])<<(i*8);return n;
}
struct Update {int side;I price,display;};
struct Tick {I ns,ms;bool valid,history;int force;std::array<I,4> price{},display{};std::vector<Update> updates;};
class Tape {
    std::ifstream in; I previous=-1;
public:
    explicit Tape(const std::string& name):in(name,std::ios::binary) {
        char magic[8];in.read(magic,8);
        if(in.gcount()!=8)throw std::runtime_error("invalid tape magic");
        TAPE_FORMAT=std::string(magic,8);
        if(TAPE_FORMAT=="BTWTP003") {
            PRICE_DECIMALS=6;MONEY_DECIMALS=10;DEPTH_KEY_MULTIPLIER=100;
            INITIAL=10000000000000LL;MIN_NOTIONAL_TICKS=5000000;MAX_NOTIONAL_TICKS=250000000;
            NATIVE_TICK_UNITS={10,1};
        }else if(TAPE_FORMAT!="BTWTP002")throw std::runtime_error("invalid tape magic");
    }
    bool next(Tick& t) {
        unsigned char h[80];in.read(reinterpret_cast<char*>(h),80);
        if(in.gcount()==0 && in.eof())return false;
        if(in.gcount()!=80)throw std::runtime_error("truncated tape header");
        t.ns=read_i64(h); t.ms=t.ns/1000000;
        if(t.ns<=previous || t.ns<=0)throw std::runtime_error("non-increasing tape clock");
        previous=t.ns;
        if(h[8]>1 || h[9]>2 || h[10]>1 || h[11]!=0)throw std::runtime_error("invalid tape flags");
        t.valid=h[8];t.force=h[9];t.history=h[10]; uint32_t n=read_u32(h+12);
        if(n>1000 || (!t.valid && n))throw std::runtime_error("invalid update count");
        for(int s=0;s<4;++s){t.price[s]=read_i64(h+16+8*s);t.display[s]=read_i64(h+48+8*s);}
        t.updates.clear();t.updates.reserve(n);
        for(uint32_t i=0;i<n;++i) {
            unsigned char u[24];in.read(reinterpret_cast<char*>(u),24);
            if(in.gcount()!=24)throw std::runtime_error("truncated update");
            for(int j=1;j<8;++j)if(u[j])throw std::runtime_error("nonzero update padding");
            Update x{int(u[0]),read_i64(u+8),read_i64(u+16)};
            if(x.side>3 || x.price<=0 || x.display<0)throw std::runtime_error("invalid update");
            t.updates.push_back(x);
        }
        if(t.valid)for(int s=0;s<4;++s)if(t.price[s]<=0 || t.display[s]<=0)
            throw std::runtime_error("invalid L1 quote");
        if(t.valid)for(int s=0;s<4;++s)if(t.price[s]%NATIVE_TICK_UNITS[s/2])
            throw std::runtime_error("L1 violates assumed native quote tick");
        return true;
    }
};
struct Level {I display=0,remaining4=0;};
struct Policy {std::string name,mode;int tp=2,sl=2,hard=0;};

struct Activity {
    I ns=0; std::array<uint32_t,2> count{};
    std::array<I,2> turnover{},age{}; I warm_mask=0;
};
class ActivityTape {
    std::ifstream in; I previous=-1;
public:
    explicit ActivityTape(const std::string& name):in(name,std::ios::binary) {
        char magic[8];in.read(magic,8);
        if(in.gcount()!=8 || std::string(magic,8)!="BTWACT01")throw std::runtime_error("invalid activity magic");
    }
    bool next(Activity& a) {
        unsigned char h[56];in.read(reinterpret_cast<char*>(h),56);
        if(in.gcount()==0 && in.eof())return false;
        if(in.gcount()!=56)throw std::runtime_error("truncated activity row");
        a.ns=read_i64(h);a.count={read_u32(h+8),read_u32(h+12)};
        a.turnover={read_i64(h+16),read_i64(h+24)};
        a.age={read_i64(h+32),read_i64(h+40)};a.warm_mask=read_i64(h+48);
        if(a.ns<=0 || a.ns<=previous || a.warm_mask<0 || a.warm_mask>3 ||
           a.turnover[0]<0 || a.turnover[1]<0 || a.age[0]<-1 || a.age[1]<-1)
            throw std::runtime_error("invalid activity row");
        previous=a.ns;return true;
    }
};
struct ActivityGate {
    std::string name; bool enabled=true;
    std::array<I,2> count{},turnover{};I max_age=0;
    // Non-exclusive diagnostics: one decision may violate several constraints.
    std::vector<std::string> failures(const Activity& a) const {
        std::vector<std::string> result;if(!enabled)return result;
        for(int v=0;v<2;++v) {
            std::string venue=VENUE[v];
            if(!(a.warm_mask&(1<<v)))result.push_back(venue+"_warm_valid");
            if(a.count[v]<count[v])result.push_back(venue+"_count");
            if(a.turnover[v]<turnover[v])result.push_back(venue+"_turnover");
            if(a.age[v]<0 || a.age[v]>max_age)result.push_back(venue+"_age");
        }
        return result;
    }
};
static I threshold_integer(const std::string& value) {
    if(value.empty())throw std::runtime_error("empty threshold integer");
    Wide n=0;for(char ch:value) {
        if(ch<'0'||ch>'9')throw std::runtime_error("invalid threshold integer");
        n=n*10+(ch-'0');if(n>std::numeric_limits<I>::max())throw std::runtime_error("threshold integer overflow");
    }return I(n);
}
static std::vector<ActivityGate> read_thresholds(const std::string& name) {
    std::ifstream in(name);std::string line;
    auto read_line=[&](){bool ok=bool(std::getline(in,line));if(!line.empty()&&line.back()=='\r')line.pop_back();return ok;};
    if(!read_line() || line!="name,countA,countB,turnoverAunits,turnoverBunits,maxAgeNs")
        throw std::runtime_error("invalid threshold CSV header");
    std::map<std::string,ActivityGate> gates;
    while(read_line()) {
        std::vector<std::string> fields;std::stringstream ss(line);std::string item;
        while(std::getline(ss,item,','))fields.push_back(item);
        if(fields.size()!=6 || line.back()==',' || (fields[0]!="q10"&&fields[0]!="q25") || gates.count(fields[0]))
            throw std::runtime_error("invalid or duplicate threshold row");
        ActivityGate g;g.name=fields[0];
        g.count={threshold_integer(fields[1]),threshold_integer(fields[2])};
        g.turnover={threshold_integer(fields[3]),threshold_integer(fields[4])};g.max_age=threshold_integer(fields[5]);
        if(g.count[0]>std::numeric_limits<uint32_t>::max()||g.count[1]>std::numeric_limits<uint32_t>::max())
            throw std::runtime_error("count threshold exceeds uint32");
        gates.emplace(g.name,g);
    }
    if(!in.eof() || gates.size()!=2)throw std::runtime_error("threshold CSV requires q10 and q25 rows");
    return {{"gateoff",false,{0,0},{0,0},0},gates.at("q10"),gates.at("q25")};
}

struct Position {
    I id=0,entry_ns=0,entry_ms=0,qty=0,initial_qty=0;
    int lng=0,parts=0;
    std::array<I,2> entry{},gross{},fees{},exit_notional_ticks{};
    I initial_net_per_coin=0,trigger_ns=0,trigger_net=0;
    I observed_max_net=std::numeric_limits<I>::min(),observed_min_net=std::numeric_limits<I>::max();
    bool initial_hard_breach=false;
    std::string pending;
};
struct Offer {int lng=0;I qty=0,ask=0,bid=0;};
class Scenario {
public:
    Policy policy;
    ActivityGate activity_gate;
    I entry_start_ns=0,entry_gated_time=0;
    I activity_pass_decisions=0,activity_fail_decisions=0,activity_rejected_entry_decisions=0,activity_rejected_entry_directions=0;
    std::map<std::string,I> activity_entry_reasons;
    std::array<std::map<I,Level>,4> budget;
    std::array<I,2> cash{INITIAL,INITIAL};
    std::unique_ptr<Position> pos;
    I ticks=0,valid_ticks=0,eligible=0,orders=0,cycles=0,wins=0,losses=0;
    I closed_net=0,total_gross=0,total_fees=0,cooldown_ms=0,next_id=0;
    I entry_gated_position=0,entry_gated_cooldown=0,entry_gated_force=0,entry_gated_halt=0;
    I depth_unavailable=0,initial_breaches=0;
    I marked_peak=2*INITIAL,marked_drawdown=0,complete_marks=0,incomplete_marks=0;
    bool stopped=false;std::string halt;
    std::map<std::string,I> exits;
    std::ofstream order_out,cycle_out,budget_out;
    explicit Scenario(Policy p,ActivityGate gate,const fs::path& out,I start_ns=0):policy(std::move(p)),activity_gate(std::move(gate)),entry_start_ns(start_ns) {
        fs::create_directory(out/policy.name);
        order_out.open(out/policy.name/"orders.csv");cycle_out.open(out/policy.name/"cycles.csv");
        if(!order_out || !cycle_out)throw std::runtime_error("cannot open scenario output");
        {
            budget_out.open(out/policy.name/"entry_budgets.jsonl");
            if(!budget_out)throw std::runtime_error("cannot open entry budget output");
        }
        order_out<<"ns,timestamp_ms,venue,side,intent,qty,price,fee,cash,cycle,exit_reason\n";
        cycle_out<<"cycle,long_venue,short_venue,qty,entry_ns,exit_ns,entry_timestamp_ms,exit_timestamp_ms,holding_ms,long_entry_price,short_entry_price,gross_pnl,total_fees,net_before_funding,bybit_net_before_funding,bitget_net_before_funding,exit_reason,exit_parts,initial_quote_net_proxy,initial_hard_budget_breach,first_trigger_ns,trigger_remaining_net_proxy,observed_max_remaining_net_proxy,observed_min_remaining_net_proxy\n";
    }
    void observe(const Tick& t) {
        for(auto& u:t.updates) {
            auto& b=budget[u.side];
            if(u.display==0){b.erase(u.price);continue;}
            auto& l=b[u.price];l.remaining4=std::max<I>(0,checked(Wide(l.remaining4)+u.display-l.display));l.display=u.display;
        }
        for(int s=0;s<4;++s) {
            if(budget[s].size()>50)throw std::runtime_error("more than 50 observed levels");
            auto it=budget[s].find(checked(Wide(t.price[s])*DEPTH_KEY_MULTIPLIER));
            if(it==budget[s].end() || it->second.display!=t.display[s])throw std::runtime_error("header L1 inconsistent with updates");
        }
    }
    I capacity(const Tick& t,int s) const {return budget[s].at(checked(Wide(t.price[s])*DEPTH_KEY_MULTIPLIER)).remaining4/4;}
    void consume(const Tick& t,int s,I q) {
        auto& l=budget[s].at(checked(Wide(t.price[s])*DEPTH_KEY_MULTIPLIER));I used=checked(Wide(q)*4);
        if(l.remaining4<used)throw std::runtime_error("budget over-consumption");l.remaining4-=used;
    }
    Offer offer(const Tick& t,int lng) const {
        int a=2*lng,b=2*(1-lng)+1;
        Offer o{lng,0,t.price[a],t.price[b]};
        I q=std::min(capacity(t,a),capacity(t,b));
        for(int v=0;v<2;++v) {
            I top=t.price[v==lng ? 2*v:2*v+1];
            Wide upper=Wide(top)*10010+10000*NATIVE_TICK_UNITS[v]; // 10bp sizing band plus one tick.
            q=std::min(q,checked(Wide(MAX_NOTIONAL_TICKS)*10000/upper));
            q=std::min(q,checked(Wide(cash[v])*10000/(upper*10003)));
        }
        q=floor10(q);if(!q)return o;
        if(Wide(q)*o.ask<MIN_NOTIONAL_TICKS || Wide(q)*o.bid<MIN_NOTIONAL_TICKS ||
           Wide(q)*o.ask>MAX_NOTIONAL_TICKS || Wide(q)*o.bid>MAX_NOTIONAL_TICKS)return o;
        if(Wide(o.bid-o.ask)*20000 < Wide(21)*(o.bid+o.ask))return o;
        o.qty=q;return o;
    }
    I net_per_coin(const Tick& t,const Position& p) const {
        I el=p.entry[p.lng],es=p.entry[1-p.lng],xl=t.price[2*p.lng+1],xs=t.price[2*(1-p.lng)];
        return checked(Wide(10000)*(xl-el+es-xs)-Wide(3)*(el+es+xl+xs));
    }
    void write_order(const Tick& t,int v,const std::string& intent,I q,I price,I fee_value,const std::string& reason) {
        bool buy=(v==pos->lng)==(intent=="entry");
        order_out<<t.ns<<','<<t.ms<<','<<VENUE[v]<<','<<(buy?"buy":"sell")<<','<<intent<<','<<q<<','<<dec(price,PRICE_DECIMALS)<<','<<dec(fee_value,MONEY_DECIMALS)<<','<<dec(cash[v],MONEY_DECIMALS)<<','<<pos->id<<','<<reason<<'\n';
        ++orders;
    }
    void enter(const Tick& t,const Offer& o) {
        pos=std::make_unique<Position>();auto& p=*pos;
        p.id=++next_id;p.entry_ns=t.ns;p.entry_ms=t.ms;p.qty=p.initial_qty=o.qty;p.lng=o.lng;
        p.entry[o.lng]=o.ask;p.entry[1-o.lng]=o.bid;
        p.initial_net_per_coin=net_per_coin(t,p);
        p.initial_hard_breach=policy.hard && Wide(p.initial_net_per_coin)*p.qty <= -Wide(p.entry[p.lng])*p.initial_qty*policy.hard;
        initial_breaches+=p.initial_hard_breach;
        if(budget_out.is_open()) {
            budget_out<<"{\"cycle_id\":"<<p.id<<",\"entry_ns\":"<<p.entry_ns<<",\"long\":\""<<VENUE[p.lng]<<"\",\"qty\":"<<p.qty<<",\"entry_prices\":{\"bybit\":"<<p.entry[0]<<",\"bitget\":"<<p.entry[1]<<"},\"entry_price_tick_size\":\""<<dec(1,PRICE_DECIMALS)<<"\",\"price_decimals\":"<<PRICE_DECIMALS<<",\"money_decimals\":"<<MONEY_DECIMALS<<",\"native_tick_units\":["<<NATIVE_TICK_UNITS[0]<<','<<NATIVE_TICK_UNITS[1]<<"],\"price_units\":\"1e-8\",\"after_entry_consumption\":false,\"pre_entry_consumption\":true,\"sides\":[";
            for(int side=0;side<4;++side){if(side)budget_out<<',';budget_out<<'[';bool first=true;
                for(auto& item:budget[side]){if(!first)budget_out<<',';first=false;budget_out<<"{\"price_units_1e8\":"<<item.first<<",\"displayed_qty\":"<<item.second.display<<",\"remaining_quarter\":"<<item.second.remaining4<<'}';}budget_out<<']';}
            budget_out<<"],\"ns\":"<<p.entry_ns<<",\"newqty\":"<<p.qty<<",\"short\":\""<<VENUE[1-p.lng]<<"\",\"prices\":[";
            for(int side=0;side<4;++side){if(side)budget_out<<',';budget_out<<t.price[side];}
            budget_out<<"],\"displayed\":[";
            for(int side=0;side<4;++side){if(side)budget_out<<',';budget_out<<t.display[side];}
            budget_out<<"]}\n";
        }
        for(int v=0;v<2;++v) {
            I f=fee(p.entry[v],p.qty);p.fees[v]=f;cash[v]-=f;
            write_order(t,v,"entry",p.qty,p.entry[v],f,"");consume(t,v==p.lng?2*v:2*v+1,p.qty);
        }

    }
    I executable_qty(const Tick& t,const Position& p) const {
        return floor10(std::min({p.qty,capacity(t,2*p.lng+1),capacity(t,2*(1-p.lng))}));
    }
    std::string risk(const Tick& t,Position& p) {
        if(!executable_qty(t,p)){++depth_unavailable;return "";}
        I n=checked(Wide(net_per_coin(t,p))*p.qty);
        p.observed_max_net=std::max(p.observed_max_net,n);p.observed_min_net=std::min(p.observed_min_net,n);
        std::string r;
        if(policy.hard) {
            I realized=checked(Wide(p.gross[0])+p.gross[1]-p.fees[0]-p.fees[1]+Wide(3)*p.qty*(p.entry[0]+p.entry[1]));
            if(Wide(realized)+n <= -Wide(p.initial_qty)*p.entry[p.lng]*policy.hard)r="hard_stop_loss";
        }
        if(r.empty()) {
            if(policy.mode=="entry_deterioration") {
                if(Wide(n)<=Wide(p.initial_net_per_coin)*p.qty-Wide(p.qty)*p.entry[p.lng]*policy.sl)r="stop_loss";
            }else if(policy.mode=="legacy_fixed_usdt") {
                if(n<=-5000000000LL)r="stop_loss";
            }else if(n<=-threshold(p.entry[p.lng],p.qty,policy.sl))r="stop_loss";
        }
        if(r.empty() && n>=threshold(p.entry[p.lng],p.qty,policy.tp))r="take_profit";
        if(!r.empty()){p.trigger_ns=t.ns;p.trigger_net=n;}
        return r;
    }
    void exit_position(const Tick& t) {
        auto& p=*pos;I q=executable_qty(t,p);
        int side0=(p.lng==0?1:0),side1=(p.lng==1?3:2);
        std::array<I,2> xp{t.price[side0],t.price[side1]};
        if(q<p.qty) {
            I minimum=10;
            for(int v=0;v<2;++v)minimum=std::max(minimum,ceildiv(ceildiv(MIN_NOTIONAL_TICKS,xp[v]),10)*10);
            if(p.qty-q<minimum)q=floor10(p.qty-minimum);
        }
        if(q<=0 || Wide(q)*xp[0]<MIN_NOTIONAL_TICKS || Wide(q)*xp[1]<MIN_NOTIONAL_TICKS){++depth_unavailable;return;}
        for(int v=0;v<2;++v) {
            I g=checked(Wide(q)*(xp[v]-p.entry[v])*10000*(v==p.lng?1:-1));I f=fee(xp[v],q);
            p.gross[v]=checked(Wide(p.gross[v])+g);p.fees[v]=checked(Wide(p.fees[v])+f);
            p.exit_notional_ticks[v]=checked(Wide(p.exit_notional_ticks[v])+Wide(q)*xp[v]);
            cash[v]=checked(Wide(cash[v])+g-f);
            write_order(t,v,"exit",q,xp[v],f,p.pending);consume(t,v==0?side0:side1,q);
        }
        p.qty-=q;++p.parts;if(p.qty)return;
        I g=checked(Wide(p.gross[0])+p.gross[1]),f=checked(Wide(p.fees[0])+p.fees[1]),n=g-f;
        cycle_out<<p.id<<','<<VENUE[p.lng]<<','<<VENUE[1-p.lng]<<','<<p.initial_qty<<','<<p.entry_ns<<','<<t.ns<<','<<p.entry_ms<<','<<t.ms<<','<<t.ms-p.entry_ms<<','<<dec(p.entry[p.lng],PRICE_DECIMALS)<<','<<dec(p.entry[1-p.lng],PRICE_DECIMALS)<<','<<dec(g,MONEY_DECIMALS)<<','<<dec(f,MONEY_DECIMALS)<<','<<dec(n,MONEY_DECIMALS)<<','<<dec(p.gross[0]-p.fees[0],MONEY_DECIMALS)<<','<<dec(p.gross[1]-p.fees[1],MONEY_DECIMALS)<<','<<p.pending<<','<<p.parts<<','<<dec(checked(Wide(p.initial_net_per_coin)*p.initial_qty),MONEY_DECIMALS)<<','<<(p.initial_hard_breach?1:0)<<','<<p.trigger_ns<<','<<dec(p.trigger_net,MONEY_DECIMALS)<<',';
        if(p.observed_max_net!=std::numeric_limits<I>::min())cycle_out<<dec(p.observed_max_net,MONEY_DECIMALS);
        cycle_out<<',';if(p.observed_min_net!=std::numeric_limits<I>::max())cycle_out<<dec(p.observed_min_net,MONEY_DECIMALS);cycle_out<<'\n';
        ++cycles;wins+=n>0;losses+=n<0;closed_net+=n;total_gross+=g;total_fees+=f;++exits[p.pending];
        cooldown_ms=t.ms+30000;pos.reset();if(Wide(cash[0])+cash[1]-2*INITIAL<=-INITIAL/100)halt="total_loss_cap";
    }
    void mark(const Tick& t) {
        I value=checked(Wide(cash[0])+cash[1]);
        if(pos) {
            if(!t.valid || capacity(t,2*pos->lng+1)<pos->qty || capacity(t,2*(1-pos->lng))<pos->qty){++incomplete_marks;return;}
            // Current cash has already paid all entry fees. Add remaining gross
            // and subtract only hypothetical close fees. No mark consumes depth.
            value=checked(Wide(value)+Wide(net_per_coin(t,*pos))*pos->qty+Wide(3)*pos->qty*(pos->entry[0]+pos->entry[1]));
        }
        ++complete_marks;marked_peak=std::max(marked_peak,value);marked_drawdown=std::max(marked_drawdown,marked_peak-value);
    }
    void tick(const Tick& t,const Activity& activity) {
        auto activity_failures=activity_gate.failures(activity);
        if(activity_failures.empty())++activity_pass_decisions;else ++activity_fail_decisions;
        ++ticks;if(t.force==2){stopped=true;halt="replay_day_end";}
        if(pos && t.force && pos->pending.empty()){pos->pending=t.force==1?"funding_guard":"replay_day_end";pos->trigger_ns=t.ns;}
        if(t.valid){++valid_ticks;observe(t);}
        std::array<Offer,2> offers;
        if(t.valid && t.history)for(int v=0;v<2;++v){offers[v]=offer(t,v);eligible+=offers[v].qty>0;}
        bool had=bool(pos);
        if(pos) {
            if(t.ms-pos->entry_ms>=60000 && pos->pending.empty()){pos->pending="holding_timeout";pos->trigger_ns=t.ns;}
            if(t.valid) {
                if(pos->pending.empty())pos->pending=risk(t,*pos);
                if(!pos->pending.empty())exit_position(t);
            }
        }
        for(auto& o:offers)if(o.qty) {
            if(had){++entry_gated_position;continue;}
            if(t.force){++entry_gated_force;continue;}
            if(stopped || !halt.empty() || cycles>=10000){++entry_gated_halt;continue;}
            if(t.ms<cooldown_ms){++entry_gated_cooldown;continue;}
            if(t.ns<entry_start_ns){++entry_gated_time;continue;}
        }
        if(had || t.force || stopped || !halt.empty() || cycles>=10000 || t.ms<cooldown_ms || t.ns<entry_start_ns){mark(t);return;}
        Offer best;for(auto& o:offers)if(o.qty && (!best.qty || Wide(o.bid-o.ask)*(best.bid+best.ask)>Wide(best.bid-best.ask)*(o.bid+o.ask)))best=o;
        if(best.qty && !activity_failures.empty()) {
            ++activity_rejected_entry_decisions;
            for(const auto& o:offers)activity_rejected_entry_directions+=o.qty>0;
            for(const auto& reason:activity_failures)++activity_entry_reasons[reason];
            mark(t);return;
        }
        if(best.qty)enter(t,best);mark(t);
    }
    void json(std::ostream& out) const {
        out<<"{\"name\":\""<<policy.name<<"\",\"mode\":\""<<policy.mode<<"\",\"tp_bps\":"<<policy.tp<<",\"sl_bps\":"<<policy.sl<<",\"hard_cap_bps\":"<<policy.hard<<",\"ticks\":"<<ticks<<",\"valid_ticks\":"<<valid_ticks<<",\"eligible_direction_evaluations\":"<<eligible<<",\"orders\":"<<orders<<",\"cycles\":"<<cycles<<",\"wins\":"<<wins<<",\"losses\":"<<losses<<",\"gross_pnl\":\""<<dec(total_gross,MONEY_DECIMALS)<<"\",\"fees\":\""<<dec(total_fees,MONEY_DECIMALS)<<"\",\"net_before_funding\":\""<<dec(closed_net,MONEY_DECIMALS)<<"\",\"cash\":{\"bybit\":\""<<dec(cash[0],MONEY_DECIMALS)<<"\",\"bitget\":\""<<dec(cash[1],MONEY_DECIMALS)<<"\"},\"unresolved_qty\":"<<(pos?pos->qty:0)<<",\"halt_reason\":\""<<halt<<"\",\"initial_hard_budget_breaches\":"<<initial_breaches<<",\"depth_unavailable\":"<<depth_unavailable<<",\"entry_gated_position\":"<<entry_gated_position<<",\"entry_gated_cooldown\":"<<entry_gated_cooldown<<",\"entry_gated_force\":"<<entry_gated_force<<",\"entry_gated_halt\":"<<entry_gated_halt<<",\"entry_gated_time\":"<<entry_gated_time<<",\"exit_reasons\":{";
        bool first=true;for(auto& e:exits){if(!first)out<<',';first=false;out<<'"'<<e.first<<"\":"<<e.second;}
        out<<"},\"activity_gate\":{\"enabled\":"<<(activity_gate.enabled?"true":"false")<<",\"countA\":"<<activity_gate.count[0]<<",\"countB\":"<<activity_gate.count[1]<<",\"turnoverAunits\":"<<activity_gate.turnover[0]<<",\"turnoverBunits\":"<<activity_gate.turnover[1]<<",\"maxAgeNs\":"<<activity_gate.max_age<<"},\"activity_pass_decisions\":"<<activity_pass_decisions<<",\"activity_fail_decisions\":"<<activity_fail_decisions<<",\"activity_rejected_entry_decisions\":"<<activity_rejected_entry_decisions<<",\"activity_rejected_entry_directions\":"<<activity_rejected_entry_directions<<",\"activity_entry_rejection_reasons\":{";
        first=true;for(const auto& r:activity_entry_reasons){if(!first)out<<',';first=false;out<<'"'<<r.first<<"\":"<<r.second;}
        out<<"},\"complete_liquidation_equity_marks\":"<<complete_marks<<",\"incomplete_liquidation_equity_marks\":"<<incomplete_marks<<",\"complete_mark_max_drawdown_usdt\":\""<<dec(marked_drawdown,MONEY_DECIMALS)<<"\",\"drawdown_caveat\":\"only simultaneous full remaining L1-budget valuations; incomplete periods omitted\"}";
    }
};
int main(int argc,char** argv) {
    try {
        std::map<std::string,std::string> options;
        if(argc!=9 && argc!=11)throw std::runtime_error("usage: cohort_replay --tape TAPE --activity ACTIVITY --thresholds CSV --output NEWDIR [--entry-start-ns NS]");
        for(int i=1;i<argc;i+=2) {
            std::string key=argv[i];
            if((key!="--tape"&&key!="--activity"&&key!="--thresholds"&&key!="--output"&&key!="--entry-start-ns")||options.count(key))
                throw std::runtime_error("unknown or duplicate option");
            options[key]=argv[i+1];
        }
        I entry_start_ns=options.count("--entry-start-ns") ? threshold_integer(options.at("--entry-start-ns")) : 0;
        auto gates=read_thresholds(options.at("--thresholds"));
        Tape tape(options.at("--tape"));ActivityTape activity_tape(options.at("--activity"));
        fs::path output(options.at("--output"));
        if(fs::exists(output))throw std::runtime_error("output already exists; refusing overwrite");
        if(!fs::create_directories(output))throw std::runtime_error("cannot create output");
        std::vector<std::unique_ptr<Scenario>> scenarios;
        for(auto& gate:gates)scenarios.push_back(std::make_unique<Scenario>(Policy{gate.name,"entry_deterioration",5,20,100},gate,output,entry_start_ns));
        Tick tick;Activity activity;I count=0,first_ns=0,last_ns=0;
        while(tape.next(tick)) {
            if(!activity_tape.next(activity))throw std::runtime_error("activity ended before tape");
            if(activity.ns!=tick.ns)throw std::runtime_error("activity/tape timestamp mismatch");
            if(!count)first_ns=tick.ns;last_ns=tick.ns;
            for(auto& s:scenarios)s->tick(tick,activity);
            if(++count%250000==0)std::cerr<<"processed "<<count<<" decisions; ns="<<tick.ns<<'\n';
        }
        if(activity_tape.next(activity))throw std::runtime_error("activity has extra rows");
        if(!count)throw std::runtime_error("empty tape");
        for(auto& s:scenarios){s->order_out.flush();s->cycle_out.flush();s->budget_out.flush();
            if(!s->order_out || !s->cycle_out || !s->budget_out)throw std::runtime_error("output write failed");}
        std::ofstream aggregate(output/"aggregate.json");
        aggregate<<"{\"status\":\"COMPLETED_SCREEN_ONLY\",\"version\":4,\"execution_enabled\":false,\"tape_format\":\""<<TAPE_FORMAT<<"\",\"price_decimals\":"<<PRICE_DECIMALS<<",\"money_decimals\":"<<MONEY_DECIMALS<<",\"native_tick_units\":["<<NATIVE_TICK_UNITS[0]<<','<<NATIVE_TICK_UNITS[1]<<"],\"activity_format\":\"BTWACT01\",\"activity_scope\":\"entry_only\",\"activity_rejection_reasons\":\"nonexclusive counts on otherwise admissible entry decisions\",\"decisions\":"<<count<<",\"entry_start_ns\":"<<entry_start_ns<<",\"first_ns\":"<<first_ns<<",\"last_ns\":"<<last_ns<<",\"fee_rate\":\"0.0003\",\"initial_cash_per_venue\":\"1000\",\"initial_exit_baseline\":\"entry_reverse_L1_quote_proxy_without_exit_budget_requirement\",\"entry_snapshots\":\"pre_entry_consumption\",\"validation\":\"requires_independent_gateoff_order_alignment_and_funding_check\",\"scenarios\":[";
        for(size_t i=0;i<scenarios.size();++i){if(i)aggregate<<',';scenarios[i]->json(aggregate);}aggregate<<"]}\n";
        aggregate.close();if(!aggregate)throw std::runtime_error("aggregate write failed");
        std::cout<<"activity replay completed: "<<count<<" decisions, "<<scenarios.size()<<" scenarios\n";return 0;
    } catch(const std::exception& e){std::cerr<<"ERROR: "<<e.what()<<'\n';return 1;}
}
