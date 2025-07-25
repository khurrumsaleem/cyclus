#include "composition.h"

#include "comp_math.h"
#include "context.h"
#include "decayer.h"
#include "error.h"
#include "recorder.h"

extern "C" {
#include "cram.hpp"
}

namespace cyclus {

int Composition::next_id_ = 1;

Composition::Ptr Composition::CreateFromAtom(CompMap v) {
  if (!compmath::ValidNucs(v)) throw ValueError("invalid nuclide in CompMap");

  if (!compmath::AllPositive(v))
    throw ValueError("negative quantity in CompMap");

  Composition::Ptr c(new Composition());
  c->atom_ = v;
  return c;
}

Composition::Ptr Composition::CreateFromMass(CompMap v) {
  if (!compmath::ValidNucs(v)) throw ValueError("invalid nuclide in CompMap");

  if (!compmath::AllPositive(v))
    throw ValueError("negative quantity in CompMap");

  Composition::Ptr c(new Composition());
  c->mass_ = v;
  return c;
}

int Composition::id() {
  return id_;
}

const CompMap& Composition::atom() {
  if (atom_.size() == 0) {
    CompMap::iterator it;
    for (it = mass_.begin(); it != mass_.end(); ++it) {
      Nuc nuc = it->first;
      atom_[nuc] = it->second / pyne::atomic_mass(nuc);
    }
  }
  return atom_;
}

const CompMap& Composition::mass() {
  if (mass_.size() == 0) {
    CompMap::iterator it;
    for (it = atom_.begin(); it != atom_.end(); ++it) {
      Nuc nuc = it->first;
      mass_[nuc] = it->second * pyne::atomic_mass(nuc);
    }
  }
  return mass_;
}

Composition::Ptr Composition::Decay(int delta, uint64_t secs_per_timestep) {
  int tot_decay = prev_decay_ + delta;
  if (decay_line_->count(tot_decay) == 1) {
    // decay_line_ has cached, pre-computed result of this decay
    return (*decay_line_)[tot_decay];
  }

  // Calculate a new decayed composition and insert it into the decay chain.
  // It will automagically appear in the decay chain for all other compositions
  // that are a part of this decay chain because decay_line_ is a pointer that
  // all compositions in the chain share.
  Composition::Ptr decayed = NewDecay(delta, secs_per_timestep);
  (*decay_line_)[tot_decay] = decayed;
  return decayed;
}

Composition::Ptr Composition::Decay(int delta) {
  return Decay(delta, kDefaultTimeStepDur);
}

void Composition::Record(Context* ctx) {
  if (recorded_) {
    return;
  }
  recorded_ = true;

  CompMap::const_iterator it;
  CompMap cm = mass();  // force lazy evaluation now
  compmath::Normalize(&cm, 1);
  for (it = cm.begin(); it != cm.end(); ++it) {
    ctx->NewDatum("Compositions")
        ->AddVal("QualId", id())
        ->AddVal("NucId", it->first)
        ->AddVal("MassFrac", it->second)
        ->Record();
  }
}

Composition::Composition() : prev_decay_(0), recorded_(false) {
  id_ = next_id_;
  next_id_++;
  decay_line_ = ChainPtr(new Chain());
}

Composition::Composition(int prev_decay, ChainPtr decay_line)
    : recorded_(false), prev_decay_(prev_decay), decay_line_(decay_line) {
  id_ = next_id_;
  next_id_++;
}

std::string Composition::ToString(CompMap v) {
  std::string comp = "";
  for (cyclus::CompMap::const_iterator it = v.begin(); it != v.end(); ++it) {
    comp += std::to_string(it->first) + std::string(": ") +
            std::to_string(it->second) + std::string("\n");
  }

  return comp;
}

Composition::Ptr Composition::NewDecay(int delta, uint64_t secs_per_timestep) {
  int tot_decay = prev_decay_ + delta;
  atom();  // force evaluation of atom-composition if not calculated already

  // the new composition is a part of this decay chain and so is created with a
  // pointer to the exact same decay_line_.
  Composition::Ptr decayed(new Composition(tot_decay, decay_line_));

  // FIXME this is only here for testing, see issue #761
  if (atom_.size() == 0) return decayed;

  // Get intial condition vector
  std::vector<double> n0(pyne_cram_transmute_info.n, 0.0);
  CompMap::const_iterator it;
  int i = -1;
  for (it = atom_.begin(); it != atom_.end(); ++it) {
    i = pyne_cram_transmute_nucid_to_i(it->first);
    if (i < 0) {
      continue;
    }
    n0[i] = it->second;
  }

  // get decay matrix
  double t = static_cast<double>(secs_per_timestep) * delta;
  std::vector<double> decay_matrix(pyne_cram_transmute_info.nnz);
  for (i = 0; i < pyne_cram_transmute_info.nnz; ++i) {
    decay_matrix[i] = -pyne_cram_transmute_info.decay_matrix[i] * t;
  }

  // perform decay
  std::vector<double> n1(pyne_cram_transmute_info.n);
  pyne_cram_expm_multiply14(decay_matrix.data(), n0.data(), n1.data());

  // convert back to map
  CompMap cm;
  for (i = 0; i < pyne_cram_transmute_info.n; ++i) {
    if (n1[i] > 0.0) {
      cm[(pyne_cram_transmute_info.nucids)[i]] = n1[i];
    }
  }
  decayed->atom_ = cm;
  return decayed;
}

}  // namespace cyclus
