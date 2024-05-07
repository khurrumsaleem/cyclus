#include "matl_sell_policy.h"

#include "error.h"

#define LG(X) LOG(LEV_##X, "selpol")
#define LGH(X)                                                    \
  LOG(LEV_##X, "selpol") << "policy " << name_ << " (agent "      \
                         << Trader::manager()->prototype() << "-" \
                         << Trader::manager()->id() << "): "

namespace cyclus {
namespace toolkit {

MatlSellPolicy::MatlSellPolicy() :
    Trader(NULL),
    name_(""),
    quantize_(0),
    throughput_(std::numeric_limits<double>::max()),
    ignore_comp_(false),
    package_id_(Package::unpackaged_id()) {
  Warn<EXPERIMENTAL_WARNING>(
      "MatlSellPolicy is experimental and its API may be subject to change");
}

MatlSellPolicy::~MatlSellPolicy() {
  if (manager() != NULL)
    manager()->context()->UnregisterTrader(this);
}

void MatlSellPolicy::set_quantize(double x) {
  assert(x >= 0);
  quantize_ = x;
}

void MatlSellPolicy::set_throughput(double x) {
  assert(x >= 0);
  throughput_ = x;
}

void MatlSellPolicy::set_ignore_comp(bool x) {
  ignore_comp_ = x;
}

void MatlSellPolicy::set_package(int x) {
  assert(x >= 1);
  package_id_ = x;
  if (manager() != NULL) {
    package_ = manager()->context()->GetPackageById(package_id_);
  } else {
    // if no real context, only unpackaged can be used.
    package_ = Package::unpackaged();
  }
}

MatlSellPolicy& MatlSellPolicy::Init(Agent* manager, ResBuf<Material>* buf,
                                     std::string name) {
  Trader::manager_ = manager;
  buf_ = buf;
  name_ = name;
  package_ = Package::unpackaged();
  return *this;
}

MatlSellPolicy& MatlSellPolicy::Init(Agent* manager, ResBuf<Material>* buf,
                                     std::string name, double throughput) {
  Trader::manager_ = manager;
  buf_ = buf;
  name_ = name;
  set_throughput(throughput);
  package_ = Package::unpackaged();
  return *this;
}

MatlSellPolicy& MatlSellPolicy::Init(Agent* manager, ResBuf<Material>* buf,
                                     std::string name, bool ignore_comp) {
  Trader::manager_ = manager;
  buf_ = buf;
  name_ = name;
  set_ignore_comp(ignore_comp);
  package_ = Package::unpackaged();
  return *this;
}

MatlSellPolicy& MatlSellPolicy::Init(Agent* manager, ResBuf<Material>* buf,
                                     std::string name, double throughput,
                                     bool ignore_comp) {
  Trader::manager_ = manager;
  buf_ = buf;
  name_ = name;
  set_throughput(throughput);
  set_ignore_comp(ignore_comp);
  package_ = Package::unpackaged();
  return *this;
}

MatlSellPolicy& MatlSellPolicy::Init(Agent* manager, ResBuf<Material>* buf,
                                     std::string name, double throughput,
                                     bool ignore_comp, double quantize,
                                     int package_id) {
  Trader::manager_ = manager;
  buf_ = buf;
  name_ = name;
  set_quantize(quantize);
  set_throughput(throughput);
  set_ignore_comp(ignore_comp);
  set_package(package_id);
  return *this;
}

MatlSellPolicy& MatlSellPolicy::Set(std::string commod) {
  commods_.insert(commod);
  return *this;
}

void MatlSellPolicy::Start() {
  if (manager() == NULL) {
    std::stringstream ss;
    ss << "No manager set on Sell Policy " << name_;
    throw ValueError(ss.str());
  }
  if (quantize_ < package_->fill_min() || quantize_ > package_->fill_max())  {
    std::stringstream ss;
    ss << "Quantize " << quantize_ << " is outside the package fill min/max values (" << package_->fill_min() << ", "
       << package_->fill_max() << ")";
    throw ValueError(ss.str());
  }
  manager()->context()->RegisterTrader(this);
}

void MatlSellPolicy::Stop() {
  if (manager() == NULL) {
    std::stringstream ss;
    ss << "No manager set on Sell Policy " << name_;
    throw ValueError(ss.str());
  }
  manager()->context()->UnregisterTrader(this);
}


double MatlSellPolicy::Limit() const {
  double bcap = buf_->quantity();
  double limit = Excl() ?                                               \
                 quantize_ * static_cast<int>(std::floor(bcap / quantize_)) : bcap;
  return std::min(throughput_, limit);
}

std::set<BidPortfolio<Material>::Ptr> MatlSellPolicy::GetMatlBids(
    CommodMap<Material>::type& commod_requests) {
  std::set<BidPortfolio<Material>::Ptr> ports;
  

  double limit = Limit();
  if (buf_->empty() || buf_->quantity() < eps()|| limit < eps())
    return ports;
  
  BidPortfolio<Material>::Ptr port(new BidPortfolio<Material>());
  CapacityConstraint<Material> cc(limit);
  port->AddConstraint(cc);
  ports.insert(port);
  LGH(INFO3) << "bidding out " << limit << " kg";

  bool excl = Excl();
  std::string commod;
  Request<Material>* req;
  Material::Ptr m, offer;
  double qty;
  int nbids;
  double package_fill;
  std::set<std::string>::iterator sit;
  std::vector<Request<Material>*>::const_iterator rit;
  for (sit = commods_.begin(); sit != commods_.end(); ++sit) {
    commod = *sit;
    if (commod_requests.count(commod) < 1)
      continue;

    const std::vector<Request<Material>*>& requests =
        commod_requests.at(commod);
    for (rit = requests.begin(); rit != requests.end(); ++rit) {
      req = *rit;
      qty = std::min(req->target()->quantity(), limit);
      package_fill = std::min(qty, package_->GetFillMass(qty));
      nbids = excl ? static_cast<int>(std::floor(qty / quantize_)) : static_cast<int>(std::floor(qty / package_fill));
      qty = excl ? quantize_ : package_fill;
      for (int i = 0; i < nbids; i++) {
        m = buf_->Pop();
        buf_->Push(m);
        offer = ignore_comp_ ? \
                Material::CreateUntracked(qty, req->target()->comp()) : \
                Material::CreateUntracked(qty, m->comp());
        port->AddBid(req, offer, this, excl);
        LG(INFO3) << "  - bid " << qty << " kg on a request for " << commod;
      }
    }
  }
  return ports;
}

void MatlSellPolicy::GetMatlTrades(
    const std::vector<Trade<Material> >& trades,
    std::vector<std::pair<Trade<Material>, Material::Ptr> >& responses) {
  Composition::Ptr c;
  std::vector<Trade<Material> >::const_iterator it;
  for (it = trades.begin(); it != trades.end(); ++it) {
    double qty = it->amt;
    LGH(INFO3) << " sending " << qty << " kg of " << it->request->commodity();
    Material::Ptr mat = buf_->Pop(qty, cyclus::eps_rsrc());
    if (ignore_comp_)
      mat->Transmute(it->request->target()->comp());
    responses.push_back(std::make_pair(*it, mat));
  }
}

}  // namespace toolkit
}  // namespace cyclus
