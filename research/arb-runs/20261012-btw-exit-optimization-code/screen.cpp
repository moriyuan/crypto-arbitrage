// Offline bounded L1 exit-policy screening. No network or exchange APIs.
// Input: BTWTP002, then little-endian <qBBBBI8q> / <B7xqq> records.
// Header L1 prices use 1e-5 ticks; update keys use exact 1e-8 price units.
// Money is exact integer nano-USDT; displayed-liquidity budgets are quarter-coins.
#include <algorithm>
#include <array>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

using I = int64_t;
using Wide = __int128_t;
namespace fs = std::filesystem;
constexpr I INITIAL = 1000000000000LL;
constexpr I MIN_NOTIONAL_TICKS = 500000;
constexpr I MAX_NOTIONAL_TICKS = 25000000;
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
        if(in.gcount()!=8 || std::string(magic,8)!="BTWTP002")throw std::runtime_error("invalid tape magic");
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
        return true;
    }
};
struct Level {I display=0,remaining4=0;};
struct Policy {std::string name,mode;int tp=2,sl=2,hard=0;};
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
    explicit Scenario(Policy p,const fs::path& out):policy(std::move(p)) {
        fs::create_directory(out/policy.name);
        order_out.open(out/policy.name/"orders.csv");cycle_out.open(out/policy.name/"cycles.csv");
        if(!order_out || !cycle_out)throw std::runtime_error("cannot open scenario output");
        if(policy.name=="baseline_net_tp2_sl2"){
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
            auto it=budget[s].find(checked(Wide(t.price[s])*1000));
            if(it==budget[s].end() || it->second.display!=t.display[s])throw std::runtime_error("header L1 inconsistent with updates");
        }
    }
    I capacity(const Tick& t,int s) const {return budget[s].at(checked(Wide(t.price[s])*1000)).remaining4/4;}
    void consume(const Tick& t,int s,I q) {
        auto& l=budget[s].at(checked(Wide(t.price[s])*1000));I used=checked(Wide(q)*4);
        if(l.remaining4<used)throw std::runtime_error("budget over-consumption");l.remaining4-=used;
    }
    Offer offer(const Tick& t,int lng) const {
        int a=2*lng,b=2*(1-lng)+1;
        Offer o{lng,0,t.price[a],t.price[b]};
        I q=std::min(capacity(t,a),capacity(t,b));
        for(int v=0;v<2;++v) {
            I top=t.price[v==lng ? 2*v:2*v+1];
            Wide upper=Wide(top)*10010+10000; // 10bp sizing band plus one tick.
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
        order_out<<t.ns<<','<<t.ms<<','<<VENUE[v]<<','<<(buy?"buy":"sell")<<','<<intent<<','<<q<<','<<dec(price,5)<<','<<dec(fee_value,9)<<','<<dec(cash[v],9)<<','<<pos->id<<','<<reason<<'\n';
        ++orders;
    }
    void enter(const Tick& t,const Offer& o) {
        pos=std::make_unique<Position>();auto& p=*pos;
        p.id=++next_id;p.entry_ns=t.ns;p.entry_ms=t.ms;p.qty=p.initial_qty=o.qty;p.lng=o.lng;
        p.entry[o.lng]=o.ask;p.entry[1-o.lng]=o.bid;
        p.initial_net_per_coin=net_per_coin(t,p);
        p.initial_hard_breach=policy.hard && Wide(p.initial_net_per_coin)*p.qty <= -Wide(p.entry[p.lng])*p.initial_qty*policy.hard;
        initial_breaches+=p.initial_hard_breach;
        for(int v=0;v<2;++v) {
            I f=fee(p.entry[v],p.qty);p.fees[v]=f;cash[v]-=f;
            write_order(t,v,"entry",p.qty,p.entry[v],f,"");consume(t,v==p.lng?2*v:2*v+1,p.qty);
        }
        if(budget_out.is_open()) {
            budget_out<<"{\"cycle_id\":"<<p.id<<",\"entry_ns\":"<<p.entry_ns<<",\"long\":\""<<VENUE[p.lng]<<"\",\"qty\":"<<p.qty<<",\"entry_prices\":{\"bybit\":"<<p.entry[0]<<",\"bitget\":"<<p.entry[1]<<"},\"entry_price_tick_size\":\"0.00001\",\"price_units\":\"1e-8\",\"after_entry_consumption\":true,\"sides\":[";
            for(int side=0;side<4;++side){if(side)budget_out<<',';budget_out<<'[';bool first=true;
                for(auto& item:budget[side]){if(!first)budget_out<<',';first=false;budget_out<<"{\"price_units_1e8\":"<<item.first<<",\"displayed_qty\":"<<item.second.display<<",\"remaining_quarter\":"<<item.second.remaining4<<'}';}budget_out<<']';}
            budget_out<<"]}\n";
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
        cycle_out<<p.id<<','<<VENUE[p.lng]<<','<<VENUE[1-p.lng]<<','<<p.initial_qty<<','<<p.entry_ns<<','<<t.ns<<','<<p.entry_ms<<','<<t.ms<<','<<t.ms-p.entry_ms<<','<<dec(p.entry[p.lng],5)<<','<<dec(p.entry[1-p.lng],5)<<','<<dec(g,9)<<','<<dec(f,9)<<','<<dec(n,9)<<','<<dec(p.gross[0]-p.fees[0],9)<<','<<dec(p.gross[1]-p.fees[1],9)<<','<<p.pending<<','<<p.parts<<','<<dec(checked(Wide(p.initial_net_per_coin)*p.initial_qty),9)<<','<<(p.initial_hard_breach?1:0)<<','<<p.trigger_ns<<','<<dec(p.trigger_net,9)<<',';
        if(p.observed_max_net!=std::numeric_limits<I>::min())cycle_out<<dec(p.observed_max_net,9);
        cycle_out<<',';if(p.observed_min_net!=std::numeric_limits<I>::max())cycle_out<<dec(p.observed_min_net,9);cycle_out<<'\n';
        ++cycles;wins+=n>0;losses+=n<0;closed_net+=n;total_gross+=g;total_fees+=f;++exits[p.pending];
        cooldown_ms=t.ms+30000;pos.reset();if(Wide(cash[0])+cash[1]-2*INITIAL<=-10000000000LL)halt="total_loss_cap";
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
    void tick(const Tick& t) {
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
        }
        if(had || t.force || stopped || !halt.empty() || cycles>=10000 || t.ms<cooldown_ms){mark(t);return;}
        Offer best;for(auto& o:offers)if(o.qty && (!best.qty || Wide(o.bid-o.ask)*(best.bid+best.ask)>Wide(best.bid-best.ask)*(o.bid+o.ask)))best=o;
        if(best.qty)enter(t,best);mark(t);
    }
    void json(std::ostream& out) const {
        out<<"{\"name\":\""<<policy.name<<"\",\"mode\":\""<<policy.mode<<"\",\"tp_bps\":"<<policy.tp<<",\"sl_bps\":"<<policy.sl<<",\"hard_cap_bps\":"<<policy.hard<<",\"ticks\":"<<ticks<<",\"valid_ticks\":"<<valid_ticks<<",\"eligible_direction_evaluations\":"<<eligible<<",\"orders\":"<<orders<<",\"cycles\":"<<cycles<<",\"wins\":"<<wins<<",\"losses\":"<<losses<<",\"gross_pnl\":\""<<dec(total_gross,9)<<"\",\"fees\":\""<<dec(total_fees,9)<<"\",\"net_before_funding\":\""<<dec(closed_net,9)<<"\",\"cash\":{\"bybit\":\""<<dec(cash[0],9)<<"\",\"bitget\":\""<<dec(cash[1],9)<<"\"},\"unresolved_qty\":"<<(pos?pos->qty:0)<<",\"halt_reason\":\""<<halt<<"\",\"initial_hard_budget_breaches\":"<<initial_breaches<<",\"depth_unavailable\":"<<depth_unavailable<<",\"entry_gated_position\":"<<entry_gated_position<<",\"entry_gated_cooldown\":"<<entry_gated_cooldown<<",\"entry_gated_force\":"<<entry_gated_force<<",\"entry_gated_halt\":"<<entry_gated_halt<<",\"exit_reasons\":{";
        bool first=true;for(auto& e:exits){if(!first)out<<',';first=false;out<<'"'<<e.first<<"\":"<<e.second;}
        out<<"},\"complete_liquidation_equity_marks\":"<<complete_marks<<",\"incomplete_liquidation_equity_marks\":"<<incomplete_marks<<",\"complete_mark_max_drawdown_usdt\":\""<<dec(marked_drawdown,9)<<"\",\"drawdown_caveat\":\"only simultaneous full remaining L1-budget valuations; incomplete periods omitted\"}";
    }
};
int main(int argc,char** argv) {
    try {
        if(argc<3 || argc>4)throw std::runtime_error("usage: screen TAPE OUTDIR [--baseline-only|--include-legacy]");
        bool baseline=false,legacy=false;
        if(argc==4){std::string option=argv[3];baseline=option=="--baseline-only";legacy=option=="--include-legacy";if(!baseline&&!legacy)throw std::runtime_error("unknown option");}
        Tape tape(argv[1]);fs::path output(argv[2]);
        if(fs::exists(output))throw std::runtime_error("output already exists; refusing overwrite");
        if(!fs::create_directories(output))throw std::runtime_error("cannot create output");
        std::vector<Policy> policies{{"baseline_net_tp2_sl2","absolute_net",2,2,0}};
        if(!baseline) {
            for(int tp:{2,5})for(int sl:{25,50,100})policies.push_back({"absolute_net_tp"+std::to_string(tp)+"_sl"+std::to_string(sl),"absolute_net",tp,sl,0});
            for(int tp:{2,5})for(int sl:{5,10,20})policies.push_back({"entry_deterioration_tp"+std::to_string(tp)+"_sl"+std::to_string(sl),"entry_deterioration",tp,sl,100});
        }
        if(legacy)policies.push_back({"legacy_net_tp1_sl5usdt","legacy_fixed_usdt",1,0,0});
        std::vector<std::unique_ptr<Scenario>> scenarios;for(auto& p:policies)scenarios.push_back(std::make_unique<Scenario>(p,output));
        Tick tick;I count=0,first_ns=0,last_ns=0;
        while(tape.next(tick)){if(!count)first_ns=tick.ns;last_ns=tick.ns;for(auto& s:scenarios)s->tick(tick);if(++count%250000==0)std::cerr<<"processed "<<count<<" decisions; ns="<<tick.ns<<'\n';}
        if(!count)throw std::runtime_error("empty tape");
        for(auto& s:scenarios){s->order_out.flush();s->cycle_out.flush();if(s->budget_out.is_open())s->budget_out.flush();if(!s->order_out || !s->cycle_out || (s->budget_out.is_open()&&!s->budget_out))throw std::runtime_error("output write failed");}
        std::ofstream aggregate(output/"aggregate.json");
        aggregate<<"{\"status\":\"COMPLETED_SCREEN_ONLY\",\"version\":2,\"execution_enabled\":false,\"tape_format\":\"BTWTP002\",\"decisions\":"<<count<<",\"first_ns\":"<<first_ns<<",\"last_ns\":"<<last_ns<<",\"fee_rate\":\"0.0003\",\"initial_cash_per_venue\":\"1000\",\"initial_exit_baseline\":\"entry_reverse_L1_quote_proxy_without_exit_budget_requirement\",\"validation\":\"requires_independent_baseline_order_alignment_and_funding_check\",\"scenarios\":[";
        for(size_t i=0;i<scenarios.size();++i){if(i)aggregate<<',';scenarios[i]->json(aggregate);}aggregate<<"]}\n";aggregate.close();if(!aggregate)throw std::runtime_error("aggregate write failed");
        std::cout<<"screen completed: "<<count<<" decisions, "<<scenarios.size()<<" scenarios\n";
        return 0;
    } catch(const std::exception& e){std::cerr<<"ERROR: "<<e.what()<<'\n';return 1;}
}
